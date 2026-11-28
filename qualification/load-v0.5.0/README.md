# Measure v0.5.0 with Inkling

This fixture starts the published release in an isolated Linux x86_64 container. The included service host and its four SIGIL compilers handle the workload. The harness uses 25 newly generated tenant credentials and a separate operations credential. All credentials and session state live in the container's temporary directory.

The provider is the local Ollama service on the Docker Desktop host at port 11435. The fixture checks the digest of `inkling-2-mini:latest` before and after the measurement. It sets `PI_MODEL=inkling-2-mini` and supplies the adapter's loopback address as `PI_ENDPOINT`. It uses the service's shipped deadlines, compiler count, quotas and retry limits.

Ollama 0.40.17 serves Anthropic Messages. For the count that v0.5.0 requires before generation, the adapter submits the same prompt, system instruction, tool definitions and thinking setting with a one-token output limit. It returns the upstream response's measured input-token count. Generation uses the original request and response bodies. This adds a model call per generation attempt and can warm the prompt cache. Provider CPU and GPU memory are outside the Linux process measurements.

## Run

Mount the checkout read-only at `/src`, the verified archive read-only at `/candidate/sigil-pi-0.5.0-linux-x86_64.tar.gz`, and a writable output directory at `/results`. Use a Linux/amd64 image with Python 3.12, the release's Z3 shared library and `pgrep`. The container must be able to reach the local Ollama service at `host.docker.internal:11435`. The service and adapter listen on container loopback.

Run both commands from `/src` inside the container.

```sh
python qualification/load-v0.5.0/run.py /candidate/sigil-pi-0.5.0-linux-x86_64.tar.gz /results/full
python qualification/load-v0.5.0/verify.py /results/full
```

Each run requires a fresh output directory name. For a two-client, 75-second compatibility run, supply `--preflight` to both commands. The full run uses 50 clients for 3,600 seconds, plus session setup and time for outstanding requests to finish. The release harness's `--smoke-test` flag records that the 25-turn forecast lacks owner approval. The fixture sets duration and concurrency independently.

The verifier reports the observed workload verdict, including failed criteria. A successful verification means the raw report, model metadata, source digests and counters reconcile. The engineering verdict also applies the declared numerical limits to the full service process tree from before setup and checks sampling coverage. The report retains a failed outcome from either assessment. The combined process figures sum current RSS, kernel threads and open descriptors for the service and its compiler children at each sample. Summed RSS can count shared pages more than once, and a five-second sampler can miss peaks between samples.
