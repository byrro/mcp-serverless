# MCP Serverless

A library for deploying MCP (Model Context Protocol) servers to serverless cloud platforms.

## Problem

Many existing MCP servers are designed to run as persistent processes — using SSE transport with long-lived connections, or stdio transport as local subprocesses — and often maintain stateful sessions (auth context, tool registrations). This makes them a poor fit for serverless platforms out of the box. Yet serverless deployment offers compelling advantages: lower cost, zero maintenance, auto-scaling, and no idle resources. This library bridges that gap by adapting MCP servers for serverless execution, handling transport adaptation, state management, and platform-specific packaging.

## Transport Strategy

MCP defines three transports, only one of which is a good fit for serverless:

- **stdio** — Local only. The client spawns the server as a child process and communicates over stdin/stdout. Not applicable to remote/serverless deployment.
- **SSE (Server-Sent Events)** — Requires a persistent, long-lived HTTP connection where the server pushes messages to the client indefinitely. Fundamentally incompatible with serverless (Lambda has a 15-minute max execution time, no persistent connections across invocations, and cold starts would break any long-lived connection).
- **Streamable HTTP** — The newest MCP transport. Uses standard HTTP request/response: the client POSTs a request, the server responds (optionally streaming the response via SSE within that single request, then the connection closes). No persistent connections needed. This maps cleanly to the serverless execution model: one invocation per request, optional response streaming, stateless between invocations.

**Streamable HTTP is the target transport for this project.** AWS Lambda supports response streaming via Function URLs (`InvokeMode: RESPONSE_STREAM`), which enables the optional per-request SSE streaming that Streamable HTTP allows. For MCP servers that currently use SSE or stdio, this library will need to adapt them to the Streamable HTTP transport as part of the deployment packaging.

## Core Goals

1. **Deploy Arcade MCP servers to AWS Lambda** as the first integration.
2. **Support multiple cloud vendors** — AWS Lambda first, with Cloudflare Workers and others to follow. The architecture should be modular so users can pick their cloud provider.
3. **Support non-Arcade MCP servers** — beyond Arcade's toolkit servers, support publicly available MCP servers built with frameworks like FastMCP or custom implementations, potentially in languages other than Python.

## Architecture Principles

- **Modular cloud provider layer**: A provider abstraction that encapsulates the specifics of each serverless platform (packaging, deployment, runtime adapter). Adding a new cloud vendor should not require changes to the core.
- **Modular MCP server source layer**: A server source abstraction that handles discovering, packaging, and configuring MCP servers from different origins (Arcade tools, FastMCP apps, generic MCP server binaries/containers).
- **Separation of concerns**: The core orchestration logic (build, configure, deploy) should be independent of both the cloud provider and the MCP server source.
- **Extensibility over completeness**: Design interfaces and extension points now, even if only one implementation exists initially. Avoid hard-coding Arcade or AWS Lambda assumptions into the core.

## Deployment Tooling

**Use boto3 (AWS SDK for Python) directly** rather than a deployment framework like Serverless Framework, AWS SAM, or CDK.

Rationale:
- This library *is* the deployment tool — wrapping another deployment framework inside it adds unnecessary indirection and coupling.
- The Serverless Framework is a Node.js CLI tool; shelling out to it from a Python library is awkward and adds an npm dependency.
- The actual AWS API surface needed is small: package code into a zip, create/update a Lambda function, configure a Function URL with response streaming, and set up an IAM execution role. A handful of boto3 calls.
- Direct SDK usage gives full control over Lambda-specific features like `InvokeMode: RESPONSE_STREAM` for Function URLs, without fighting framework abstractions.
- The provider abstraction layer means the implementation can be swapped later (e.g., to CDK or Pulumi) if infrastructure complexity grows, without changing the public API.

## Scope

### First milestone
- Deploy an Arcade MCP server to AWS Lambda.
- Provide a CLI or programmatic API to configure and trigger the deployment.

### Future milestones
- Cloudflare Workers support.
- FastMCP server support.
- Generic MCP server support (arbitrary languages/runtimes).
- Infrastructure-as-code integration (e.g., CDK, Terraform).

## Reference Documentation

Local copies of relevant documentation are maintained in `./external/`:
- `external/arcade-docs/` — Arcade documentation (scraped from docs.arcade.dev)
- `external/aws-lambda/` — AWS Lambda Developer Guide (scraped from AWS docs)
