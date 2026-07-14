# Architecture

Snow AI Studio is a small internal Flask application. It deliberately uses a layered monolith: one deployable application, one worker process, and clear module boundaries without a distributed-service framework.

## Source layout

```text
imagegen/
  app.py              Flask application factory and process-wide hooks
  config/             validated, hot-reloadable channel and chat configuration
  integrations/       OpenAI-compatible HTTP clients and image adapters
  services/           business operations and transaction boundaries
  web/                Flask routes, authorization, and HTTP serialization
  models.py           SQLAlchemy persistence model
  serializers.py      database model to public API payload conversion
  storage.py          image validation and filesystem persistence
  worker.py           queue claiming, provider execution, and settlement
config/                compatibility defaults used before admin-managed config exists
static/                browser assets grouped by css, js, assets, and vendor
templates/             base template, pages, and shared partials
tests/integration/     end-to-end service and HTTP contract tests
```

## Dependency rules

```text
web -> services -> models/storage
web -> serializers
services -> config and integrations
worker -> services and integrations
config -> repository/models
integrations -> config value objects
```

- Services do not import Flask routes or request globals.
- Integrations do not commit database transactions.
- Routes validate HTTP shapes, then delegate business decisions to services.
- API keys only exist in encrypted configuration storage and server-side config objects.
- `imagegen.services` is the stable service import surface; individual modules are implementation details.

## Transaction ownership

- Account, billing, workspace, conversation, and generation services own their database commits.
- The worker locks the user and generation item before settlement.
- Image files are written before the matching database commit and removed on rollback.
- Runtime configuration is saved as one versioned document with optimistic revision checks.

## Extension guide

To add an image channel, prefer implementing an adapter in `imagegen/integrations/` and registering it in `ProviderFactory`. Channel capabilities, price, models, and concurrency belong to admin-managed configuration, not route code.

To add a user workflow, add the business operation to the relevant service first, expose a thin route in `imagegen/web/`, then cover the service and HTTP contract in `tests/integration/`.

Do not add a repository abstraction around SQLAlchemy unless a second persistence implementation is actually required. The current services are the transaction boundary and keep this internal application easy to trace.
