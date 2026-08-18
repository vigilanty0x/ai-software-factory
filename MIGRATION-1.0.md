# Migration vers AI Software Factory 1.0.0

AI Software Factory 1.0.0 conserve le legacy gate 0.1 tout en ajoutant le runtime durable DAG, SQLite, replay, receipts et publication bornée déjà documentés dans le changelog.

## Compatibilité

- Produit/distribution : `ai-software-factory`.
- Namespace Python : `ai_software_factory`.
- CLI : `ai-software-factory`.
- `evaluate(record)` et le mode legacy `ai-software-factory record.json` restent disponibles avec les codes 0/2 historiques.

## Release gates

Le candidat 1.0 est `PREPARED`, pas publié. Avant toute publication : CI Ubuntu/Windows/macOS sur Python 3.11-3.13, wheel+sdist installables, suite complète, contre-preuves legacy et runtime, smoke CLI hors checkout, sdist auto-auditable, SHA-256, CycloneDX 1.6, provenance SLSA vérifiée, compatibilité consommateurs, décision de publication et vérification post-publication.

## Rollback

Rollback vers 0.1.0 pour le gate legacy si une future publication 1.0 échoue. Les données SQLite 1.0 et les exports de preuve ne doivent pas être présentés comme rétrocompatibles avec 0.1 : préserver les artefacts 1.0 pour audit et revenir à un workspace 0.1 séparé plutôt que réécrire la preuve.

## Archive gate

`ai-software-factory-starter-kit` reste consolidé sous `packages/` mais n'est pas archivé par cette migration. L'archivage exige inventaire consommateurs, compatibilité/redirect, rollback et approbation humaine explicite.
