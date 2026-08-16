# AI assistance disclosure

AI systems may assist with design exploration, implementation, tests, and
documentation in this repository. Assistance does not replace verification.

Every accepted change is expected to have:

- a human-readable requirement and trust-boundary explanation;
- repository-local tests exercising the changed behavior;
- fresh execution results from the integrated code;
- review for fabricated APIs, unsafe defaults, hidden network access, private
  data, and copied material;
- clear attribution when external source material is used.

Release verification also builds both wheel and source distribution, installs
the wheel without runtime dependencies, and exercises its CLI outside the
source checkout.

The project itself is provider-neutral. It contains no model credentials, model
SDK, hidden prompt, telemetry, or network call. The deterministic mock exists so
orchestration can be tested without pretending that a remote model responded.
