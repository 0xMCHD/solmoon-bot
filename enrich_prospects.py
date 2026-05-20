"""
Enrich prospects_dijon.csv with public business data.

Source : https://recherche-entreprises.api.gouv.fr
         (API officielle gouvernement, gratuite, sans authentification)

USAGE :
    python3 enrich_prospects.py [--input prospects_dijon.csv] [--output prospects_enriched.csv]

Le script ajoute pour chaque ligne :
    - siren                : numéro SIREN
    - siret                : numéro SIRET (établissement principal)
    - adresse_complete     : adresse au format "X RUE DE Y 21000 DIJON"
    - dirigeant_nom        : "PRENOM NOM" du dirigeant principal
    - dirigeant_role       : "PRESIDENT" / "GERANT" / etc.
    - dirigeant_naissance  : année de naissance (utile pour différencier homonymes)
    - naf_code             : code NAF de l'activité
    - naf_libelle          : libellé de l'activité officielle
    - tranche_effectif     : nombre de salariés (souvent "0" pour nouvelles boîtes)
    - linkedin_search_url  : URL LinkedIn pré-formatée pour chercher le dirigeant
    - google_search_url    : URL Google pour trouver email/téléphone

Le mail direct n'est PAS dans cette API (RGPD). Mais avec le nom + ville du
dirigeant, ton outreach LinkedIn devient 3-5x plus efficace.

Pour récupérer les vrais emails, étape 2 : Pappers API gratuite
(signup sur api.pappers.fr → token gratuit 100 req/jour).
"""

import argparse
import csv
import time
from urllib.parse import quote_plus

import httpx

API_URL = "https://recherche-entreprises.api.gouv.fr/search"


def search_company(client: httpx.Client, name: str, cp: str = "", ville: str = "") -> dict | None:
    """Search for a company by name + optional postal code / city."""
    params = {"q": name, "per_page": 1}
    if cp:
        params["code_postal"] = cp
    elif ville:
        params["q"] = f"{name} {ville}"

    try:
        resp = client.get(API_URL, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results", [])
        if not results:
            # Retry without postal code if first try failed
            if cp:
                params.pop("code_postal", None)
                params["q"] = f"{name} {ville}" if ville else name
                resp = client.get(API_URL, params=params, timeout=10)
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
        return results[0] if results else None
    except Exception as e:
        return None


def extract_fields(company: dict) -> dict:
    """Extract the useful fields from API response."""
    if not company:
        return {}

    # Dirigeants — pick the first one (usually president/gérant)
    dirigeants = company.get("dirigeants", []) or []
    dirigeant_nom = ""
    dirigeant_role = ""
    dirigeant_naissance = ""
    if dirigeants:
        d = dirigeants[0]
        prenoms = d.get("prenoms", "") or ""
        nom = d.get("nom", "") or ""
        dirigeant_nom = f"{prenoms} {nom}".strip()
        dirigeant_role = d.get("qualite", "") or ""
        dirigeant_naissance = str(d.get("annee_de_naissance", "") or "")

    # Address — prefer siège, fall back to first matching establishment
    siege = company.get("siege", {}) or {}
    adresse = siege.get("adresse", "") or ""
    cp = siege.get("code_postal", "") or ""
    commune = siege.get("libelle_commune", "") or ""
    adresse_complete = f"{adresse} {cp} {commune}".strip().replace("  ", " ")

    # NAF
    naf_code = company.get("activite_principale", "") or ""
    naf_libelle = siege.get("activite_principale", "") or naf_code

    # Tranche effectif
    tranche = company.get("tranche_effectif_salarie", "") or "0"

    return {
        "siren": company.get("siren", "") or "",
        "siret": siege.get("siret", "") or "",
        "adresse_complete": adresse_complete,
        "dirigeant_nom": dirigeant_nom,
        "dirigeant_role": dirigeant_role,
        "dirigeant_naissance": dirigeant_naissance,
        "naf_code": naf_code,
        "naf_libelle": naf_libelle,
        "tranche_effectif": tranche,
    }


def build_search_urls(dirigeant: str, entreprise: str, ville: str) -> dict:
    """Build pre-formatted search URLs for LinkedIn + Google."""
    urls = {}

    # LinkedIn — search the dirigeant first (best signal), fallback to company
    if dirigeant:
        q = quote_plus(f"{dirigeant} {ville}")
        urls["linkedin_search_url"] = f"https://www.linkedin.com/search/results/people/?keywords={q}"
    else:
        q = quote_plus(f"{entreprise} {ville}")
        urls["linkedin_search_url"] = f"https://www.linkedin.com/search/results/companies/?keywords={q}"

    # Google — search for email or phone
    q_g = quote_plus(f'"{entreprise}" {ville} contact email')
    urls["google_search_url"] = f"https://www.google.com/search?q={q_g}"

    return urls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="prospects_dijon.csv")
    parser.add_argument("--output", default="prospects_enriched.csv")
    parser.add_argument("--limit", type=int, default=0, help="Test on first N rows (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.5, help="Sleep between API calls (politeness)")
    args = parser.parse_args()

    # Load input CSV
    with open(args.input, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if args.limit > 0:
        rows = rows[:args.limit]

    print(f"📂 {len(rows)} prospects à enrichir depuis {args.input}")
    print(f"   API : recherche-entreprises.api.gouv.fr (gratuit, sans auth)")
    print()

    enriched_rows = []
    found = 0
    not_found = 0
    errors = 0

    with httpx.Client(timeout=15) as client:
        for i, row in enumerate(rows, 1):
            nom = row.get("nom_entreprise", "")
            cp = row.get("cp", "")
            ville = row.get("ville", "")

            if not nom:
                not_found += 1
                enriched_rows.append(row)
                continue

            # Clean the name for search (remove punctuation that breaks API)
            search_name = nom.replace(",", " ").replace("'", " ").strip()

            try:
                company = search_company(client, search_name, cp=cp, ville=ville)
            except Exception:
                company = None
                errors += 1

            if company:
                fields = extract_fields(company)
                urls = build_search_urls(fields["dirigeant_nom"], nom, ville)
                row.update(fields)
                row.update(urls)
                found += 1
                marker = "✓"
            else:
                # Still add empty fields + search URLs for manual lookup
                urls = build_search_urls("", nom, ville)
                row.update({
                    "siren": "", "siret": "", "adresse_complete": "",
                    "dirigeant_nom": "", "dirigeant_role": "", "dirigeant_naissance": "",
                    "naf_code": "", "naf_libelle": "", "tranche_effectif": "",
                })
                row.update(urls)
                not_found += 1
                marker = "✗"

            enriched_rows.append(row)
            print(f"  {marker} [{i:>3}/{len(rows)}] {nom[:50]}")

            time.sleep(args.sleep)

    # Write output CSV with extended schema
    base_fields = ["date_creation", "type_entreprise", "nom_entreprise",
                   "activite", "ville", "cp", "categorie_prospect",
                   "score_priorite", "url_bodacc"]
    new_fields = ["siren", "siret", "adresse_complete",
                  "dirigeant_nom", "dirigeant_role", "dirigeant_naissance",
                  "naf_code", "naf_libelle", "tranche_effectif",
                  "linkedin_search_url", "google_search_url"]
    fieldnames = base_fields + new_fields

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in enriched_rows:
            writer.writerow(row)

    print()
    print("─" * 60)
    print(f"✅ Enrichissement terminé : {args.output}")
    print(f"   Trouvés : {found}/{len(rows)} ({found*100//max(1,len(rows))}%)")
    print(f"   Non trouvés : {not_found}")
    if errors:
        print(f"   Erreurs API : {errors}")
    print()
    print("💡 Pour les emails directs, étape 2 :")
    print("   1. Signup gratuit sur https://api.pappers.fr")
    print("   2. Récupère ton token (100 req/jour offerts)")
    print("   3. On code enrich_pappers.py qui ajoute les emails dirigeants")


if __name__ == "__main__":
    main()
