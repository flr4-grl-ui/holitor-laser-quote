# Cahier des charges — Configurateur de découpe laser pour Shopify

## 1. Objectif

Permettre à un visiteur de déposer un fichier DXF et d'obtenir immédiatement un prix de fabrication et de livraison, puis de payer via Shopify.

## 2. Parcours client

1. Dépôt du DXF
2. Aperçu graphique
3. Choix matière : S355JR ou inox 304L
4. Choix épaisseur
5. Quantité
6. Code postal
7. Analyse géométrique
8. Prix instantané
9. Validation
10. Checkout Shopify
11. Commande + DXF disponibles pour la production

## 3. Données extraites du DXF

- dimensions hors tout
- surface nette
- longueur totale de coupe
- contours fermés
- trous
- amorçages estimés
- éventuelles anomalies

## 4. Formule V1

Poids unitaire :
`surface_nette_mm² × épaisseur_mm / 1e9 × densité_kg_m³`

Matière :
`poids × prix_kg × coefficient_chute`

Découpe :
`longueur_découpe / vitesse_mm_min × tarif_machine_min`

Amorçages :
`nombre_contours × prix_amorçage`

Production :
`mise_en_route + quantité × (matière + découpe + amorçages)`

Prix de vente :
`max(minimum_commande, production × coefficient_marge)`

## 5. Transport

Dimensions colis estimées à partir des dimensions de la pièce, de l'épaisseur empilée et du calage.

Poids volumétrique :
`L_cm × l_cm × h_cm / coefficient_transporteur`

Poids facturable :
`max(poids_réel, poids_volumétrique)`

Règles :
- grille colis selon poids facturable
- bascule palette au-delà des limites de poids ou dimensions
- règles spécifiques à ajouter pour Corse, îles et international

## 6. Règles de sécurité commerciale

Pas de prix automatique si :
- aucun contour fermé fiable
- géométrie 3D
- plusieurs pièces distinctes ambiguës
- dimensions hors capacité
- unité douteuse
- entités non prises en charge qui changent vraisemblablement la géométrie

Dans ce cas : « contrôle technique nécessaire ».

## 7. Back-office

Paramètres modifiables sans changer le code :
- matériaux
- densités
- prix/kg
- épaisseurs
- vitesses laser
- tarif machine/min
- amorçage
- coût de préparation
- chute matière
- marge
- minimum de commande
- règles de livraison

## 8. Shopify

Architecture cible :
- configurateur hébergé séparément
- Theme App Extension / App Block côté boutique
- API privée côté serveur
- stockage DXF privé
- base de données des devis
- création d'une Draft Order
- redirection checkout Shopify
- webhook après paiement

Informations de commande :
- ID devis
- nom fichier original
- référence stockage
- matériau
- épaisseur
- quantité
- dimensions
- poids
- longueur de coupe
- prix
- livraison

## 9. Phases

### Phase 1 — Prototype
Analyse DXF et calcul local. **Incluse dans ce dossier.**

### Phase 2 — Calibration
Tarifs et règles réelles de l'entreprise.

### Phase 3 — Shopify
Authentification, Draft Orders, checkout et webhooks.

### Phase 4 — Production
Stockage DXF, fiche atelier, historique et administration.

### Phase 5 — Extensions
Aluminium, pliage, taraudage, ébavurage, peinture, galvanisation, nesting.
