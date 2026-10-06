# Catalog

Our real infrastructure catalog lives here: `hosts.yaml`, `services.yaml`, `platform.yaml` (any
`*.yaml` files are merged).

- Start with `infra-agent kb discover`, which writes drafts to `_discovered/` (ignored by the agent).
- Field guide: [docs/knowledge-base.md](../../docs/knowledge-base.md).
- Working example: [examples/demo/catalog/](../../examples/demo/catalog/).
- Check changes with `infra-agent kb validate --live`.
