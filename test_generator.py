"""Quick test: analyze two leads live, generate messages, save to data/leads_test.csv."""
import pandas as pd

from scraper.web_analyzer import analyze
from ai.message_generator import generate

RAW_LEADS = [
    {
        "name": "Somos Robinjud Servicios Legales",
        "city": "Crevillent",
        "website": "http://www.somosrobinjud.com/",
        "phone": "965703255",
        "address": "C/ Pósito, Crevillent",
        "zip_code": "03330",
        "province": "Alicante",
    },
    {
        "name": "Talens & San Emeterio Abogados",
        "city": "Crevillent",
        "website": "http://www.talensysanemeterioabogados.com/",
        "phone": "965270777",
        "address": "Paseo Fontenay, 2-Bajo, Crevillent",
        "zip_code": "03330",
        "province": "Alicante",
    },
]

OUTPUT = "data/leads_test.csv"


def main():
    leads = []
    for i, raw in enumerate(RAW_LEADS, 1):
        print(f"\n[{i}/{len(RAW_LEADS)}] Analyzing: {raw['name']}")
        lead = analyze(raw)
        print(f"  CMS       : {lead['cms']}")
        print(f"  SEO score : {lead['seo_score']}")
        print(f"  SEO issues: {lead['seo_issues']}")
        print(f"  Email     : {lead.get('email', '')}")

        print(f"  Generating message...")
        msg = generate(lead)
        lead["subject"] = msg["subject"]
        lead["body"] = msg["body"]
        leads.append(lead)
        print(f"  Subject   : {msg['subject']}")
        print(f"  Body      :\n{msg['body']}")

    pd.DataFrame(leads).to_csv(OUTPUT, index=False)
    print(f"\n[+] Saved {len(leads)} leads → {OUTPUT}")


if __name__ == "__main__":
    main()
