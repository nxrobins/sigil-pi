"""Fact-only TypeSafe adapter owned by the sigil-pi research host.

Transport uses the existing audited, secret-injected SIGIL forge. The Wave
sidecar validates typed answers against its original request and applies its
0.8 proof floor; no generated text or host normalization can raise a score.
"""
import json
import math


def build_body(request, model):
    if request.get("role") != "Judge" or request.get("state", {}).get("task") not in (
            "verify_fact_support", "review_stored_fact"):
        raise ValueError("Jev is restricted to fact_support and fact_revision")
    questions = request.get("questions")
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 256:
        raise ValueError("invalid judgment questions")
    wire = {}
    for key, question in questions.items():
        kind = question["type"]
        body = {"type": kind, "instructions": question["instructions"]}
        if kind == "choice":
            options = question["options"]
            if not 2 <= len(options) <= 255 or len({o["key"] for o in options}) != len(options):
                raise ValueError("invalid choices")
            body["criteria"] = {o["key"]: o.get("description") for o in options}
        elif kind == "noul":
            criteria = question.get("criteria") or {}
            body["criteria"] = {k: criteria[v] for k, v in
                                (("true", "yes"), ("false", "no")) if v in criteria}
        elif kind == "score":
            body["criteria"] = question["levels"]
        else:
            raise ValueError("unknown judgment type")
        wire[key] = body
    body = {"state": request["state"], "model": model, "questions": wire}
    if len(json.dumps(body).encode()) > 128 * 1024:
        raise ValueError("judgment request exceeds host byte budget")
    return body


def parse_response(payload, request):
    answers = payload["answers"]
    if set(answers) != set(request["questions"]):
        raise ValueError("incomplete or unexpected provider answers")
    out = {}
    for key, answer in answers.items():
        question = request["questions"][key]
        kind = answer["type"]
        if kind != question["type"]:
            raise ValueError("judgment answer type mismatch")
        if kind == "noul":
            values = [answer["noul"]]
            out[key] = {"type": kind, "probability": answer["noul"]}
        else:
            probabilities = answer["probabilities"]
            expected = ({o["key"] for o in question["options"]} if kind == "choice"
                        else {str(i) for i in range(len(question["levels"]))})
            if set(probabilities) != expected:
                raise ValueError("incomplete probability distribution")
            values = [*probabilities.values(), answer["confidence"]]
            if abs(sum(probabilities.values()) - 1) > 0.05:
                raise ValueError("probability distribution does not sum to one")
            if kind == "choice":
                if answer["choice"] not in expected:
                    raise ValueError("unknown choice")
                out[key] = {k: answer[k] for k in ("type", "choice", "probabilities", "confidence")}
            else:
                score = answer["score"]
                if not isinstance(score, (float, int)) or not math.isfinite(score) or not 0 <= score <= len(expected)-1:
                    raise ValueError("invalid score")
                out[key] = {k: answer[k] for k in ("type", "score", "probabilities", "confidence")}
        if not all(isinstance(v, (float, int)) and not isinstance(v, bool)
                   and math.isfinite(v) and 0 <= v <= 1 for v in values):
            raise ValueError("invalid native confidence/probability")
    usage = payload.get("usage") or {}
    return {"answers": out, "usage": {"prompt_tokens": usage.get("input_tokens"),
                                     "completion_tokens": usage.get("output_tokens")}}


class FactJudgment:
    def __init__(self, agent, api_key, endpoint="https://api.typesafe.ai/v1/systemone",
                 model="jev-latest"):
        from agent import _endpoint_host
        if not api_key:
            raise ValueError("fact grounding requires a provider credential")
        self.agent, self.api_key, self.endpoint, self.model = agent, api_key, endpoint, model
        self.host = _endpoint_host(endpoint)
        self.source = None

    def __call__(self, request):
        from agent import PI_ROOT, MEMORY_SESSION, _llm_error_code, _llm_error_is_transient
        body = build_body(request, self.model)
        if self.source is None:
            self.source = self.agent._compose((PI_ROOT / "tools/agent_turn.sigil").read_text(), ["http"])
        header = "Authorization: Bearer {{secret:jev}}\nContent-Type: application/json"
        for attempt in range(3):
            output, error = self.agent._forge(
                self.source, f"{self.endpoint}|{header}|{json.dumps(body, ensure_ascii=False)}",
                {"net": [self.host], "secret": [f"jev={self.api_key}"]},
                kind="fact_revision" if request["state"]["task"] == "review_stored_fact" else "fact_support",
                session=MEMORY_SESSION)
            if not error:
                return parse_response(json.loads(output), request)
            if attempt == 2 or not _llm_error_is_transient(_llm_error_code(error)):
                return {"unavailable": True}
            self.agent._sleep_before_retry(0.25 * 2 ** attempt)
        return {"unavailable": True}
