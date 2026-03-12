# Planning Issues

Open questions and decisions to resolve before writing the implementation plan.

---

## 1. Understanding Arcade MCP Servers

Before we can deploy Arcade MCP servers to Lambda, we need to understand how they work today.

- **How are Arcade MCP servers currently run?** What's the entry point? What transport do they use (SSE, Streamable HTTP, stdio)?
- **Do they run standalone, or do they proxy to the Arcade Engine?** If they call back to `engine.arcade.dev` (or a self-hosted engine) for tool execution, the Lambda function is a thin adapter. If they run the full tool logic locally, the Lambda package needs to include all tool dependencies.
- **How is auth handled?** Does the MCP server validate client tokens itself, or delegate to the Arcade Engine? What credentials does the server need (API keys, service tokens)?
- **What does an Arcade MCP server's dependency footprint look like?** Is it lightweight (just the Arcade SDK) or heavy (many tool-specific dependencies)? This affects Lambda packaging and cold start times.
- **Can we run an Arcade MCP server with Streamable HTTP transport today, or do we need to adapt it?**

*Action: Read the Arcade docs on MCP server setup and hosting.*

---

## 2. Streamable HTTP Transport Spec

We declared Streamable HTTP as the target transport, but we need to understand its exact contract.

- **What HTTP endpoints does it expose?** What methods (GET, POST, DELETE)?
- **What does the request/response format look like?** JSON-RPC over HTTP? What headers are required?
- **How does session management work?** Does the spec require a session ID? Is it truly stateless per-request, or does initialization create server-side state that must persist across requests?
- **How does the optional SSE streaming within a response work?** When does the server choose to stream vs return a single response?
- **Is there a reference implementation we can study?**

*Action: Read the MCP Streamable HTTP transport specification.*

---

## 3. Lambda Runtime Architecture

How should the Lambda function be structured?

- **Function URL vs API Gateway?** Function URLs are simpler and support response streaming directly. API Gateway adds features (custom domains, rate limiting, WAF) but has a 30-second timeout and adds complexity. Which is the right default? Should we support both?
- **Packaging format: zip vs container image?** Zip is simpler and faster to deploy but has a 250MB uncompressed limit. Container images support up to 10GB and allow arbitrary runtimes (useful for non-Python MCP servers later). Which should we start with?
- **Python runtime version?** Which Lambda Python runtime to target (3.11, 3.12, 3.13)?
- **How to handle dependencies?** Lambda zip deployments need dependencies bundled. Do we create a Lambda Layer for common dependencies, or bundle everything into the function zip?
- **Cold start mitigation?** Should we support Provisioned Concurrency configuration? Or is that a future concern?
- **Memory and timeout defaults?** What are sensible defaults for an MCP server?

---

## 4. State Management

MCP has a concept of sessions. Serverless is stateless. How do we reconcile this?

- **Does the Streamable HTTP transport require server-side session state?** If the server must remember state from `initialize` when handling subsequent `tools/call` requests, we have a problem.
- **If state is needed, where do we store it?** Options: DynamoDB, ElastiCache, or encode state in the client (session token / JWT). Each has different cost, latency, and complexity tradeoffs.
- **Can we avoid state entirely?** If each request carries all the context the server needs (tool definitions, auth tokens), the Lambda can be truly stateless. Is this possible with the MCP spec?

---

## 5. Library Public API

How do users interact with this library?

- **CLI, programmatic API, or both?** A CLI is great for quick deployments; a programmatic API enables integration with other tools and CI/CD. Starting with one and adding the other later is fine — but which first?
- **Configuration format?** YAML file, TOML file, Python code, CLI flags, or a combination? How does the user specify: which MCP server to deploy, which cloud provider, what credentials, what resource settings (memory, timeout)?
- **What's the deployment lifecycle?** Just `deploy`, or also `status`, `update`, `destroy`, `logs`?
- **How does the user specify which Arcade tools/toolkits to include?** By name? By configuration file?

---

## 6. Authentication and Security

Multiple auth concerns at different layers.

- **Client → MCP server auth**: How do clients authenticate to the deployed Lambda MCP server? API key in a header? OAuth? No auth (public)? The Function URL is publicly accessible by default — what's the security story?
- **MCP server → Arcade Engine auth**: The deployed server likely needs an Arcade API key to call the engine. How is this key provided? Lambda environment variable? AWS Secrets Manager?
- **AWS credentials for deployment**: The library needs AWS credentials to deploy Lambda functions. Do we assume `~/.aws/credentials` / environment variables, or provide explicit configuration?
- **Least privilege IAM**: What's the minimum IAM policy needed for deployment? What IAM role does the Lambda function itself need?

---

## 7. Packaging and Dependency Management

How do we build the Lambda deployment artifact?

- **How do we resolve and bundle Python dependencies?** Use `pip install --target`? Use a Docker container to build Linux-compatible wheels (needed when deploying from macOS)?
- **Do we need to handle native/compiled dependencies?** Some Python packages have C extensions that must be compiled for Amazon Linux. How do we handle this cross-compilation?
- **How do we keep the package small?** Strip unnecessary files, use Lambda Layers for common dependencies?
- **Where does the packaging happen?** Locally on the user's machine, or in a Docker container for reproducibility?

---

## 8. Project Structure and Distribution

How is this library itself structured and distributed?

- **Package name?** `mcp-serverless`? `mcpctl`? Something else for PyPI?
- **Minimum Python version for the library itself?** (Not the Lambda runtime — the user's machine running the CLI/API.)
- **Dependency management?** `pyproject.toml` with what build system (hatchling, setuptools, flit)?
- **Monorepo structure?** The library, CLI, and provider implementations — all in one package, or separate packages?

---

## 9. Testing Strategy

How do we test this?

- **Unit tests**: Core logic, provider abstraction, packaging logic.
- **Integration tests**: Actually deploying to AWS Lambda and invoking the function. These need real AWS credentials and cost money. How do we handle this in CI?
- **Local testing**: Can users test their MCP server locally before deploying? Should we provide a local emulation mode (e.g., using Docker or a local HTTP server)?
- **How do we test the Lambda function handler itself?** A local test harness that simulates Lambda invocation events?

---

## 10. Non-Arcade Server Support (Future, but Affects Architecture Now)

We want to support non-Arcade MCP servers eventually. Even though it's a future milestone, some architectural decisions now could make this much easier or harder.

- **How do generic MCP servers differ from Arcade ones?** They might not call the Arcade Engine, might use different transports, might be written in other languages.
- **For non-Python servers (e.g., TypeScript FastMCP), what Lambda runtime do we use?** Node.js? Container image with arbitrary runtime? This is relevant to the zip-vs-container decision above.
- **Should the provider abstraction be language-agnostic from the start?** Or is it OK to assume Python initially and generalize later?
- **How do we discover and configure third-party MCP servers?** npm packages? Docker images? Git repos?

---

## Priority

Not all of these need answers before we start. Suggested priority:

**Must resolve before writing the plan:**
1. Understanding Arcade MCP servers (#1) — foundational
2. Streamable HTTP transport spec (#2) — defines the runtime contract
3. Lambda runtime architecture (#3) — major structural decisions
4. State management (#4) — could fundamentally change the architecture

**Should resolve, but can make provisional decisions:**
5. Library public API (#5) — affects project structure but can iterate
6. Auth and security (#6) — needs a strategy, details can be refined
7. Packaging (#7) — practical but well-understood problem space

**Can defer:**
8. Project structure and distribution (#8) — standard Python project decisions
9. Testing strategy (#9) — follows from implementation decisions
10. Non-Arcade support (#10) — future milestone, just keep the door open
