# Holitor Laser Quote

Backend FastAPI pour le configurateur de découpe laser Shopify.

## Fonctionnement

- reçoit le DXF côté serveur ;
- recalcule la géométrie et le prix ;
- refuse les DXF nécessitant un contrôle manuel ;
- crée un Draft Order Shopify avec **une seule ligne par fichier DXF** ;
- la ligne Shopify a **quantité = 1** ;
- le prix de cette ligne est le **montant total TTC exact du devis** ;
- renvoie `invoiceUrl`, le lien de checkout Shopify.

## Variables Render

- `SHOPIFY_SHOP_DOMAIN=gmuucv-ex.myshopify.com`
- `SHOPIFY_API_VERSION=2026-07`
- `SHOPIFY_ADMIN_ACCESS_TOKEN=...`
- `ALLOWED_ORIGINS=https://gmuucv-ex.myshopify.com`

Le jeton Shopify doit provenir d'une application personnalisée disposant au minimum de `write_draft_orders`.

## Render

Le dépôt contient `render.yaml`. Une fois les fichiers commités sur GitHub, Render peut déployer le service automatiquement.

Build : `pip install -r requirements.txt`

Start : `uvicorn main:app --host 0.0.0.0 --port $PORT`

Health check : `/health`

## API

### POST /api/quote
Form-data : `file`, `material`, `thickness_mm`, `quantity`, `postal_code`.

### POST /api/checkout
Même form-data. Recalcule le DXF, crée le Draft Order et renvoie `checkout_url`.

> Note TVA : cette V1 encode le total TTC comme prix d'une ligne personnalisée non taxable afin de garantir un checkout identique au devis. Avant mise en production définitive, il faudra valider le traitement comptable/TVA avec la configuration Shopify de Holitor.
