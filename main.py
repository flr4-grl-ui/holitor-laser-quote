from __future__ import annotations

import io
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Iterable

import ezdxf
from ezdxf.path import make_path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union
import httpx

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "pricing.json"

app = FastAPI(title="Holitor Laser Quote")

_allowed = os.getenv("ALLOWED_ORIGINS", "https://gmuucv-ex.myshopify.com").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in _allowed if x.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def iter_entities(entity):
    """Décompose les INSERT simples et retourne les entités géométriques."""
    if entity.dxftype() == "INSERT":
        try:
            for child in entity.virtual_entities():
                yield from iter_entities(child)
        except Exception:
            return
    else:
        yield entity


SUPPORTED = {
    "LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE",
    "LWPOLYLINE", "POLYLINE"
}


def entity_to_points(entity, tolerance_mm=0.20):
    """
    Convertit une entité DXF 2D en polyligne approchée.
    ezdxf conserve les arcs/bulges puis les aplatit avec une tolérance.
    """
    try:
        path = make_path(entity)
        pts = [(float(v.x), float(v.y)) for v in path.flattening(distance=tolerance_mm)]
        if len(pts) < 2:
            return None, False
        closed = bool(getattr(path, "is_closed", False))
        if closed and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts, closed
    except Exception:
        return None, False


def canonical_ring_key(coords, decimals=3):
    """Déduplique un même contour vu comme extérieur ou trou."""
    pts = [(round(x, decimals), round(y, decimals)) for x, y in coords]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    edges = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        edges.append(tuple(sorted((a, b))))
    return tuple(sorted(edges))


def analyse_dxf(data: bytes):
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "Fichier DXF trop volumineux (15 Mo max pour la V1).")

    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=True) as f:
        f.write(data)
        f.flush()
        try:
            doc = ezdxf.readfile(f.name)
        except Exception as exc:
            raise HTTPException(400, f"DXF illisible : {exc}")

    msp = doc.modelspace()

    linework = []
    all_points = []
    ignored = {}
    z_warning = False

    for top in msp:
        for entity in iter_entities(top):
            kind = entity.dxftype()
            if kind not in SUPPORTED:
                ignored[kind] = ignored.get(kind, 0) + 1
                continue

            # Alerte Z pour quelques entités usuelles.
            try:
                if kind == "LINE":
                    if abs(entity.dxf.start.z) > 0.01 or abs(entity.dxf.end.z) > 0.01:
                        z_warning = True
            except Exception:
                pass

            pts, closed = entity_to_points(entity)
            if not pts:
                ignored[kind] = ignored.get(kind, 0) + 1
                continue

            all_points.extend(pts)
            linework.append(LineString(pts))

    if not linework or not all_points:
        raise HTTPException(400, "Aucune géométrie 2D exploitable trouvée dans le DXF.")

    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    width_mm = maxx - minx
    height_mm = maxy - miny

    cut_length_mm = sum(g.length for g in linework)

    # On nœude les lignes puis on polygonise : cela sait reconstruire un contour
    # même s'il est constitué de plusieurs entités LINE.
    noded = unary_union(linework)
    faces = list(polygonize(noded))

    if not faces:
        return {
            "status": "manual_review",
            "reason": "Aucun contour fermé fiable n'a pu être reconstruit.",
            "width_mm": width_mm,
            "height_mm": height_mm,
            "cut_length_mm": cut_length_mm,
            "ignored_entities": ignored,
            "z_warning": z_warning,
            "segments": linework,
            "bbox": [minx, miny, maxx, maxy],
        }

    # Règle pair/impair de profondeur :
    # - niveau 0 = matière
    # - niveau 1 = trou
    # - niveau 2 = îlot de matière, etc.
    material_faces = []
    exterior_polygons = [Polygon(p.exterior) for p in faces]
    for i, face in enumerate(faces):
        rp = face.representative_point()
        depth = 0
        for j, ext in enumerate(exterior_polygons):
            if i == j:
                continue
            if ext.contains(rp):
                depth += 1
        if depth % 2 == 0:
            material_faces.append(face)

    net_area_mm2 = sum(p.area for p in material_faces)

    # Nombre de contours uniques = approximation utile du nombre de perçages.
    ring_keys = set()
    for face in faces:
        k = canonical_ring_key(list(face.exterior.coords))
        if k:
            ring_keys.add(k)
        for hole in face.interiors:
            k = canonical_ring_key(list(hole.coords))
            if k:
                ring_keys.add(k)
    pierces = max(1, len(ring_keys))

    # Plusieurs îlots de matière distincts sont acceptés mais déclenchent une alerte.
    disconnected = len(material_faces) > 1

    status = "ok"
    reasons = []
    if z_warning:
        status = "manual_review"
        reasons.append("Des coordonnées Z non nulles ont été détectées.")
    if disconnected:
        status = "manual_review"
        reasons.append("Plusieurs zones de matière distinctes ont été détectées.")
    if width_mm <= 0 or height_mm <= 0 or net_area_mm2 <= 0:
        status = "manual_review"
        reasons.append("Dimensions ou surface incohérentes.")
    if width_mm > 3000 or height_mm > 1500:
        status = "manual_review"
        reasons.append("La pièce dépasse la capacité machine configurée (3000 × 1500 mm).")

    return {
        "status": status,
        "reason": " ".join(reasons) if reasons else None,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "net_area_mm2": net_area_mm2,
        "cut_length_mm": cut_length_mm,
        "pierces": pierces,
        "ignored_entities": ignored,
        "z_warning": z_warning,
        "segments": linework,
        "bbox": [minx, miny, maxx, maxy],
    }


def make_preview_svg(segments, bbox):
    minx, miny, maxx, maxy = bbox
    w = max(maxx - minx, 1.0)
    h = max(maxy - miny, 1.0)
    pad = max(w, h) * 0.05 + 1
    view = f"{minx-pad} {-maxy-pad} {w+2*pad} {h+2*pad}"
    paths = []
    for geom in segments:
        coords = list(geom.coords)
        d = " ".join(
            (("M" if i == 0 else "L") + f" {x:.3f} {-y:.3f}")
            for i, (x, y) in enumerate(coords)
        )
        paths.append(f'<path d="{d}" fill="none" stroke="currentColor" stroke-width="{max(w,h)/500:.3f}"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" '
        f'preserveAspectRatio="xMidYMid meet" class="dxf-svg">'
        + "".join(paths) + "</svg>"
    )


def calculate_shipping(cfg, width_mm, height_mm, thickness_mm, qty, total_piece_weight_kg):
    s = cfg["shipping"]
    padding = s["padding_mm"]
    L = max(width_mm + padding, 120)
    W = max(height_mm + padding, 120)
    H = max(thickness_mm * qty + padding, s["minimum_height_mm"])

    actual_kg = total_piece_weight_kg + s["packaging_weight_kg"]
    volumetric_kg = (L / 10) * (W / 10) * (H / 10) / s["volumetric_divisor_cm3_per_kg"]
    chargeable = max(actual_kg, volumetric_kg)

    parcel_ok = (
        max(L, W, H) <= s["parcel_max_side_mm"]
        and actual_kg <= s["parcel_max_weight_kg"]
    )

    if parcel_ok:
        for tier in s["tiers"]:
            if chargeable <= tier["max_kg"]:
                return {
                    "mode": "colis",
                    "price_ex_vat": tier["price"],
                    "actual_weight_kg": actual_kg,
                    "volumetric_weight_kg": volumetric_kg,
                    "chargeable_weight_kg": chargeable,
                    "package_mm": [L, W, H],
                }

    price = s["pallet_base_price"] + max(0, actual_kg - 30) * s["pallet_price_per_kg_above_30"]
    return {
        "mode": "palette",
        "price_ex_vat": round(price, 2),
        "actual_weight_kg": actual_kg,
        "volumetric_weight_kg": volumetric_kg,
        "chargeable_weight_kg": chargeable,
        "package_mm": [L, W, H],
    }


def quote_from_analysis(cfg, a, material, thickness_mm, qty, postal_code):
    if material not in cfg["materials"]:
        raise HTTPException(400, "Matériau inconnu.")

    mat = cfg["materials"][material]
    key = f"{thickness_mm:g}"
    if key not in mat["thicknesses_mm"]:
        raise HTTPException(400, "Épaisseur non disponible pour ce matériau.")

    if qty < 1 or qty > 10000:
        raise HTTPException(400, "Quantité invalide.")

    density = mat["density_kg_m3"]
    # mm² × mm = mm³ ; 1 m³ = 1e9 mm³
    unit_weight_kg = a["net_area_mm2"] * thickness_mm / 1_000_000_000 * density
    total_weight_kg = unit_weight_kg * qty

    pp = cfg["pricing"]
    speed_mm_min = mat["thicknesses_mm"][key]

    material_unit_cost = (
        unit_weight_kg * mat["price_eur_kg"] * pp["material_waste_factor"]
    )
    cut_min_unit = a["cut_length_mm"] / speed_mm_min
    machine_unit_cost = cut_min_unit * pp["machine_rate_per_min"]
    pierce_unit_cost = a["pierces"] * pp["pierce_cost"]

    variable_unit_cost = material_unit_cost + machine_unit_cost + pierce_unit_cost
    production_cost = pp["setup_fee"] + qty * variable_unit_cost
    selling_ex_vat = max(
        pp["minimum_order_ex_vat"],
        production_cost * pp["sales_margin_factor"]
    )

    shipping = calculate_shipping(
        cfg, a["width_mm"], a["height_mm"], thickness_mm, qty, total_weight_kg
    )

    # La V1 limite le calcul de transport à la France métropolitaine.
    shipping_note = None
    pc = "".join(c for c in postal_code if c.isdigit())[:5]
    if len(pc) != 5:
        shipping_note = "Code postal à vérifier."
    elif pc.startswith("20"):
        shipping_note = "Corse : tarif transport à valider manuellement."

    vat = cfg["vat_rate"]
    goods_vat = selling_ex_vat * vat
    shipping_vat = shipping["price_ex_vat"] * vat
    total_ex_vat = selling_ex_vat + shipping["price_ex_vat"]
    total_inc_vat = total_ex_vat * (1 + vat)

    return {
        "currency": cfg["currency"],
        "material": material,
        "material_label": mat["label"],
        "thickness_mm": thickness_mm,
        "quantity": qty,
        "postal_code": postal_code,
        "geometry": {
            "width_mm": round(a["width_mm"], 2),
            "height_mm": round(a["height_mm"], 2),
            "net_area_mm2": round(a["net_area_mm2"], 2),
            "cut_length_mm": round(a["cut_length_mm"], 2),
            "pierces": a["pierces"],
        },
        "weight": {
            "unit_kg": round(unit_weight_kg, 3),
            "total_kg": round(total_weight_kg, 3),
        },
        "production": {
            "cut_time_unit_min": round(cut_min_unit, 3),
            "material_unit_cost": round(material_unit_cost, 2),
            "machine_unit_cost": round(machine_unit_cost, 2),
            "pierce_unit_cost": round(pierce_unit_cost, 2),
            "setup_fee": pp["setup_fee"],
            "selling_price_ex_vat": round(selling_ex_vat, 2),
            "unit_selling_price_ex_vat": round(selling_ex_vat / qty, 2),
        },
        "shipping": {
            **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in shipping.items()},
            "note": shipping_note,
        },
        "totals": {
            "goods_ex_vat": round(selling_ex_vat, 2),
            "shipping_ex_vat": round(shipping["price_ex_vat"], 2),
            "total_ex_vat": round(total_ex_vat, 2),
            "vat": round(goods_vat + shipping_vat, 2),
            "total_inc_vat": round(total_inc_vat, 2),
        },
    }


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/config")
def public_config():
    cfg = load_config()
    return {
        "materials": {
            code: {
                "label": data["label"],
                "thicknesses_mm": [float(x) for x in data["thicknesses_mm"].keys()],
            }
            for code, data in cfg["materials"].items()
        }
    }


@app.post("/api/quote")
async def quote(
    file: UploadFile = File(...),
    material: str = Form(...),
    thickness_mm: float = Form(...),
    quantity: int = Form(...),
    postal_code: str = Form(""),
):
    if not file.filename or not file.filename.lower().endswith(".dxf"):
        raise HTTPException(400, "Veuillez déposer un fichier .DXF.")

    data = await file.read()
    a = analyse_dxf(data)
    preview_svg = make_preview_svg(a["segments"], a["bbox"])

    # Les données shapely ne doivent pas sortir dans le JSON.
    a_public = {k: v for k, v in a.items() if k not in {"segments", "bbox"}}

    if a["status"] != "ok":
        return {
            "status": "manual_review",
            "analysis": a_public,
            "preview_svg": preview_svg,
        }

    cfg = load_config()
    q = quote_from_analysis(cfg, a, material, thickness_mm, quantity, postal_code)
    return {
        "status": "ok",
        "analysis": a_public,
        "quote": q,
        "preview_svg": preview_svg,
    }


def _shopify_settings():
    domain = os.getenv("SHOPIFY_SHOP_DOMAIN", "").strip().replace("https://", "").rstrip("/")
    token = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "").strip()
    version = os.getenv("SHOPIFY_API_VERSION", "2026-07").strip()
    if not domain or not token:
        raise HTTPException(503, "Connexion Shopify non configurée sur le serveur.")
    return domain, token, version


async def create_shopify_draft_order(*, filename: str, quote: dict, analysis: dict):
    domain, token, version = _shopify_settings()
    endpoint = f"https://{domain}/admin/api/{version}/graphql.json"

    total_ttc = quote["totals"]["total_inc_vat"]
    q = quote
    attrs = [
        {"key": "Fichier DXF", "value": filename},
        {"key": "Matière", "value": q["material_label"]},
        {"key": "Épaisseur", "value": f'{q["thickness_mm"]:g} mm'},
        {"key": "Quantité pièces", "value": str(q["quantity"])},
        {"key": "Dimensions", "value": f'{q["geometry"]["width_mm"]} × {q["geometry"]["height_mm"]} mm'},
        {"key": "Poids unitaire", "value": f'{q["weight"]["unit_kg"]} kg'},
        {"key": "Longueur de coupe", "value": f'{q["geometry"]["cut_length_mm"]} mm'},
        {"key": "Amorçages", "value": str(q["geometry"]["pierces"])},
        {"key": "Code postal", "value": q["postal_code"]},
        {"key": "Fabrication HT", "value": f'{q["totals"]["goods_ex_vat"]:.2f} EUR'},
        {"key": "Transport HT", "value": f'{q["totals"]["shipping_ex_vat"]:.2f} EUR'},
        {"key": "TVA incluse", "value": f'{q["totals"]["vat"]:.2f} EUR'},
        {"key": "Total TTC", "value": f'{total_ttc:.2f} EUR'},
    ]

    mutation = """
    mutation CreateLaserDraft($input: DraftOrderInput!) {
      draftOrderCreate(input: $input) {
        draftOrder { id name invoiceUrl }
        userErrors { field message }
      }
    }
    """
    variables = {
        "input": {
            "presentmentCurrencyCode": "EUR",
            "acceptAutomaticDiscounts": False,
            "allowDiscountCodesInCheckout": False,
            "taxExempt": True,
            "tags": ["decoupe-laser", "DXF", "configurateur"],
            "note": (
                f"Devis calculé automatiquement. Total TTC {total_ttc:.2f} EUR, "
                f"dont TVA indicative {q['totals']['vat']:.2f} EUR. "
                "Le prix de la ligne est TTC pour garantir un checkout identique au devis."
            ),
            "lineItems": [{
                "title": f"Découpe laser – {filename}",
                "quantity": 1,
                "originalUnitPriceWithCurrency": {
                    "amount": f"{total_ttc:.2f}",
                    "currencyCode": "EUR"
                },
                "requiresShipping": False,
                "taxable": False,
                "sku": "LASER-DXF-CUSTOM",
                "customAttributes": attrs,
                "weight": {
                    "value": max(float(q["weight"]["total_kg"]), 0.001),
                    "unit": "KILOGRAMS"
                }
            }]
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
            json={"query": mutation, "variables": variables},
        )
    if resp.status_code >= 400:
        raise HTTPException(502, f"Erreur Shopify HTTP {resp.status_code}.")
    payload = resp.json()
    if payload.get("errors"):
        raise HTTPException(502, f"Erreur Shopify GraphQL : {payload['errors'][0].get('message', 'inconnue')}")
    out = payload.get("data", {}).get("draftOrderCreate", {})
    if out.get("userErrors"):
        raise HTTPException(502, "Erreur Shopify : " + "; ".join(e.get("message", "") for e in out["userErrors"]))
    draft = out.get("draftOrder")
    if not draft or not draft.get("invoiceUrl"):
        raise HTTPException(502, "Shopify n'a pas retourné de lien de checkout.")
    return draft


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/checkout")
async def checkout(
    request: Request,
    file: UploadFile = File(...),
    material: str = Form(...),
    thickness_mm: float = Form(...),
    quantity: int = Form(...),
    postal_code: str = Form(""),
):
    if not file.filename or not file.filename.lower().endswith(".dxf"):
        raise HTTPException(400, "Veuillez déposer un fichier .DXF.")

    data = await file.read()
    a = analyse_dxf(data)
    if a["status"] != "ok":
        raise HTTPException(422, a.get("reason") or "Le DXF nécessite un contrôle technique.")

    cfg = load_config()
    quote = quote_from_analysis(cfg, a, material, thickness_mm, quantity, postal_code)
    draft = await create_shopify_draft_order(
        filename=file.filename, quote=quote, analysis=a
    )
    return {
        "status": "ok",
        "draft_order_id": draft["id"],
        "draft_order_name": draft.get("name"),
        "checkout_url": draft["invoiceUrl"],
        "quote": quote,
    }
