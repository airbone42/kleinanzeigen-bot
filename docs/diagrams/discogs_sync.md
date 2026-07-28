# Discogs Sync Graph

```mermaid
graph TD
    A[fetch_inventory] --> B[filter_treasures]
    B --> C[sync_kleinanzeigen]
    C --> D((END))
```
