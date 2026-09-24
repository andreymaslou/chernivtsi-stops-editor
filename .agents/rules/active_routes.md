---
description: "Whitelist of active transit routes in Chernivtsi. No other routes should be generated, routed, or displayed."
---

# Active Routes Rule

**Context:** The public transport network of Chernivtsi has a strictly defined set of active routes. 

**Rule:** When generating graphs, parsing live data, scraping, or editing routes, you **MUST ONLY** process the routes listed below. Any other routes (e.g., historical, temporary merged routes like "11/3", or deprecated ones) must be considered invalid and should NOT be added to `global_routes.json`, `graph.json`, or the frontend.

## Allowed Bus Routes (30 total)
`1`, `3`, `4`, `5`, `6`, `7`, `8`, `8A`, `9`, `9A`, `10`, `10A`, `13`, `15`, `15K`, `19`, `20`, `21`, `23`, `24`, `25`, `26`, `27`, `29`, `33`, `34`, `36`, `37`, `39`, `43`

## Allowed Trolleybus Routes (8 total)
`1`, `2`, `3`, `4`, `5`, `6`, `6A`, `8`

### Important Constraints
- **Strict Matching:** Drop or ignore any data containing routes not explicitly present in this list.
- **Trolleybus 3:** Must be referred to simply as `3` (not `11/3` or any other temporary alias).
- **Modification:** Do not modify this whitelist without explicit permission from the user.
