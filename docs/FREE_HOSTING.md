# Optional free remote hosting — evaluation (nothing deployed)

**Budget: $0.** No paid service, billing account, GCP project or paid API call was used or created. **No remote deployment has been
attempted or verified**, so none is claimed. The local Docker Compose stack is the authoritative release.
The sources below are public docs/web results gathered 2026-11-23; re-verify plan terms before relying on them.

## Why nothing was deployed
Every candidate needs an account sign-up/login (GitHub app authorisation, Prefect Horizon, Neo4j Aura) that only the owner can do, and the
database half is the hard part. Deployment is therefore a documented, owner-run optional step with go/no-go gates, below.

## Candidates
| Option | Fit | Findings / open questions |
|---|---|---|
| **FastMCP Horizon (Prefect), free personal tier** — preferred | Hosts a FastMCP server from a linked GitHub repo at `https://<project>.fastmcp.app/mcp`; auth is on by default (Horizon OAuth); free for personal projects, no card ([FastMCP docs](https://gofastmcp.com/docs/deployment/prefect-horizon), [Horizon](https://www.prefect.io/horizon)) | Unverified: how its OAuth gateway interacts with this app's own JWT verification (we must **not** drop app-level auth just because the host has some); entrypoint contract (server object vs. our `create_app` factory); free-tier call quota (one third-party source says ~20K calls/month, not confirmed by Prefect); whether outbound Bolt to Aura is allowed |
| **Render Free** — fallback | Docker web service; spins down after 15 min idle, ~up to 1 min wake, 750 instance-hours/month, ephemeral disk ([Render docs](https://render.com/docs/free)) | Works with the existing `runtime` image. Cold starts of a minute are tolerable for a demo; keep-alive pinging is discouraged by Render and is not done |
| **Neo4j AuraDB Free** — database | One database per instance, shared tenancy, no SLA ([Aura limits](https://neo4j.com/docs/aura/quotas-and-limits/)); predefined read-only "viewer" role reportedly available on Free ([changelog](https://aura.feedback.neo4j.com/changelog/new-database-roles-and-seamless-access-to-aura-instances)) | **Go/no-go gate:** the server may only receive a *read-only* login. If Aura Free cannot issue a viewer-role user whose password differs from the admin one, **do not deploy** (that would expose admin credentials). Custom roles and extra databases are not available on Free |

## Gates before claiming a remote deployment (all must pass)
1. A read-only Aura user exists; a write attempt with it is denied (record the denial). Seed with the admin credential from the owner's machine only.
2. Server environment contains only the read-only credentials (host secret store, never in the repo).
3. Auth stays enabled and verified end-to-end: no token → 401; expired/wrong-audience → 401; valid → tools work. If the host's OAuth cannot be combined with
   the app's JWT check without disabling either, stop: local remains authoritative.
4. `python -m eval.smoke --url <remote>/mcp` passes from outside; tool discovery, execution and typed errors verified; results saved under `eval/results/`.
5. Only then add the URL to the README, labelled "free-tier demo, no availability guarantee".

## Free-tier risks to state when it is used
Cold starts, quota exhaustion, instance pausing/expiry on the free database, shared tenancy, no SLA, no high availability.
