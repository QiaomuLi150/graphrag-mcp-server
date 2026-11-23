# Client demonstration (captured transcript)

Output of `docker compose run --rm demo` against the local Neo4j Community stack (JSON truncated for display; request ids vary per run).

```text
Discovered 4 tools at http://server:8080/mcp: find_relationship_paths, get_entity, retrieve_graph_context, search_entities

## Find an entity by (partial) name
> search_entities({"query": "ledger", "limit": 3})
{
  "entities": [
    {
      "id": "svc-ledger-service",
      "name": "Ledger Service",
      "labels": [
        "Service"
      ],
      "description": "Deployed backend service.",
      "aliases": [
        "Ledger"
      ],
      "evidence": [
        {
          "source_id": "src-002",
          "chunk_id": "ch-0008",
          "source_uri": "demo://docs/src-002"
        },
        {
          "source_id": "src-006",
          "chunk_id": "ch-0024",
          "source_uri": "demo://docs/src-006"
        },
        {
          "source_id": "src-007",
          "chunk_id": "ch-0025",
          "source_uri": "demo://docs/src-007"
        }
      ]
    },
    {
      "id": "svc-ledger-api",
      "name": "Ledger API",
      "labels": [
        "Service"
      ],
      "description": "Deployed backend service.",
      "aliases": [
        "LedgerAPI",
        "ledger-api"
      ],
      "evidence": [
        {
          "source_id": "src-001",
          "chunk_id": "ch-0004",
          "source_uri": "demo://docs/src-001"
        },
        {
          "source_id": "src-002",
          "chunk_id": "ch-0005",
          "source_uri": "demo://docs/src-002"
        },
        {
          "source_id": "src-002",
          "chunk_id": "ch-0007",
          "source_uri": "demo://docs/src-002"
        }
      ]
    },
    {
      "id": "db-ledger-key-value-store",
      "name": "Ledger  

## Inspect it with attributed neighbours
> get_entity({"entity_id": "svc-ledger-service", "neighbor_limit": 3})
{
  "entity": {
    "id": "svc-ledger-service",
    "name": "Ledger Service",
    "labels": [
      "Service"
    ],
    "description": "Deployed backend service.",
    "aliases": [
      "Ledger"
    ],
    "evidence": [
      {
        "source_id": "src-002",
        "chunk_id": "ch-0008",
        "source_uri": "demo://docs/src-002"
      },
      {
        "source_id": "src-006",
        "chunk_id": "ch-0024",
        "source_uri": "demo://docs/src-006"
      },
      {
        "source_id": "src-007",
        "chunk_id": "ch-0025",
        "source_uri": "demo://docs/src-007"
      }
    ]
  },
  "neighbors": [
    {
      "relationship_id": "e-34a900a6f324",
      "relationship_type": "DEPENDS_ON",
      "direction": "in",
      "entity_id": "svc-checkout-processor",
      "entity_name": "Checkout Processor",
      "entity_labels": [
        "Service"
      ],
      "statement": "Checkout Processor depends on Ledger Service.",
      "evidence": [
        {
          "source_id": "src-006",
          "chunk_id": "ch-0024",
          "source_uri": "demo://docs/src-006"
        }
      ]
    },
    {
      "relationship_id": "e-e107d98b3417",
      "relationship_type": "DEPENDS_ON",
      "direction": "in",
      "entity_id": "svc-checkout-worker",
      "entity_name": "Checkout Worker",
      "entity_labels": [
        "Service"
      ],
      "statement": "Checkout Worker dep 

## How are two things connected (multi-hop)?
> find_relationship_paths({"source_entity_id": "feat-gift-cards", "target_entity_id": "svc-ledger-service", "max_hops": 3, "max_paths": 2})
{
  "paths": [
    {
      "node_ids": [
        "feat-gift-cards",
        "svc-ledger-api",
        "svc-edge-gateway-service",
        "svc-ledger-service"
      ],
      "node_names": [
        "Gift Cards",
        "Ledger API",
        "Edge Gateway Service",
        "Ledger Service"
      ],
      "relationship_ids": [
        "e-17e4bfe122f8",
        "e-8dadc20f3e68",
        "e-5b74bb509560"
      ],
      "relationship_types": [
        "IMPLEMENTED_BY",
        "DEPENDS_ON",
        "DEPENDS_ON"
      ],
      "directions": [
        "out",
        "out",
        "out"
      ],
      "evidence": [
        {
          "source_id": "src-001",
          "chunk_id": "ch-0004",
          "source_uri": "demo://docs/src-001"
        },
        {
          "source_id": "src-008",
          "chunk_id": "ch-0031",
          "source_uri": "demo://docs/src-008"
        },
        {
          "source_id": "src-010",
          "chunk_id": "ch-0038",
          "source_uri": "demo://docs/src-010"
        }
      ]
    },
    {
      "node_ids": [
        "feat-gift-cards",
        "svc-ledger-api",
        "svc-webhook-api",
        "svc-ledger-service"
      ],
      "node_names": [
        "Gift Cards",
        "Ledger API",
        "Webhook API",
        "Ledger Service"
      ],
      "relationship_ids": [
        "e-17e4bfe122f8",
        "e-f4874add9a07",
        "e-80f94d3b2 

## Ask in natural language, get evidence
> retrieve_graph_context({"query": "How is Partner Gateway Service related to Ledger Service?", "top_k": 3, "max_chars": 800})
{
  "snippets": [
    {
      "text": "Partner Gateway Service depends on Ledger Service.",
      "entity_ids": [
        "svc-partner-gateway-service",
        "svc-ledger-service"
      ],
      "relationship_id": "e-6d0f0815433b",
      "evidence": [
        {
          "source_id": "src-012",
          "chunk_id": "ch-0047",
          "source_uri": "demo://docs/src-012"
        }
      ]
    },
    {
      "text": "Edge Gateway Service depends on Partner Gateway Service.",
      "entity_ids": [
        "svc-edge-gateway-service",
        "svc-partner-gateway-service"
      ],
      "relationship_id": "e-ae2762784b50",
      "evidence": [
        {
          "source_id": "src-008",
          "chunk_id": "ch-0031",
          "source_uri": "demo://docs/src-008"
        }
      ]
    },
    {
      "text": "Partner Gateway Service integrates with Ledger Service.",
      "entity_ids": [
        "svc-partner-gateway-service",
        "svc-ledger-service"
      ],
      "relationship_id": "e-cf557826d4e6",
      "evidence": [
        {
          "source_id": "src-012",
          "chunk_id": "ch-0048",
          "source_uri": "demo://docs/src-012"
        }
      ]
    },
    {
      "text": "Edge Gateway Service depends on Ledger Service.",
      "entity_ids": [
        "svc-edge-gateway-service",
        "svc-ledger-service"
      ],
      "relationship_id": "e-5b74bb509560",
    

## Unsupported question -> abstains
> retrieve_graph_context({"query": "What is the airspeed of an unladen swallow?"})
{
  "snippets": [],
  "linked_entities": [],
  "paths": [],
  "sufficient_evidence": false,
  "meta": {
    "request_id": "f8e1cc124cf5456891e101cb8b9bc55f",
    "graph_revision": "demo-fbbbfb9663a7feee",
    "truncated": false,
    "latency_ms": 3.04,
    "contract_version": "1.0"
  }
} 

## Bad input -> typed error
> get_entity({"entity_id": "svc-does-not-exist"})
ERROR: {"code":"NOT_FOUND","message":"entity 'svc-does-not-exist' not found","retryable":false,"request_id":"31faa3f7c8df4313afdd97ebea5a2ee0"} 

```
