#!/usr/bin/env python3
"""Met à jour le data.json d'un dashboard client SPK depuis l'API Meta Marketing.

Usage : python scripts/update_dashboard.py <ad_account_id> <dossier_dashboard> [client]
Variables d'environnement : META_TOKEN (obligatoire), META_API_VERSION (défaut v23.0).

Périmètre : campagnes "actuelles" = dépense sur les 3 derniers jours, ou date de fin
à venir (statut actif), plus les campagnes terminées depuis moins de 7 jours.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
API = "https://graph.facebook.com/" + os.environ.get("META_API_VERSION", "v23.0")
TOKEN = os.environ.get("META_TOKEN", "")

OBJECTIFS = {
    "OUTCOME_ENGAGEMENT": "Interactions", "OUTCOME_AWARENESS": "Notoriété",
    "OUTCOME_TRAFFIC": "Trafic", "OUTCOME_LEADS": "Prospects", "OUTCOME_SALES": "Ventes",
    "OUTCOME_APP_PROMOTION": "Promotion d'app", "VIDEO_VIEWS": "Vues vidéo", "REACH": "Couverture",
}
PLATEFORMES = {"facebook": "Facebook", "instagram": "Instagram", "audience_network": "Audience Network",
               "messenger": "Messenger", "threads": "Threads"}
METRICS = "spend,impressions,reach,inline_link_clicks,actions,video_play_actions,video_thruplay_watched_actions"


def get(path, **params):
    """GET Graph API avec pagination ; renvoie la liste complète de `data`."""
    params["access_token"] = TOKEN
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    out = []
    while url:
        with urllib.request.urlopen(url, timeout=60) as r:
            body = json.load(r)
        if "error" in body:
            raise RuntimeError(body["error"].get("message"))
        if "data" not in body:
            return body
        out.extend(body["data"])
        url = body.get("paging", {}).get("next")
    return out


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def action(row, key, action_type=None):
    """Somme d'une liste d'actions Meta (ex. video_thruplay_watched_actions)."""
    vals = row.get(key) or []
    return sum(num(a.get("value")) for a in vals if action_type is None or a.get("action_type") == action_type)


def metrics(row):
    return {
        "depense": round(num(row.get("spend")), 2),
        "impressions": int(num(row.get("impressions"))),
        "couverture": int(num(row.get("reach"))),
        "clics_lien": int(num(row.get("inline_link_clicks"))),
        "lectures_video": int(action(row, "video_play_actions")),
        "vues_video": int(action(row, "video_thruplay_watched_actions")),
        "engagements": int(action(row, "actions", "post_engagement")),
    }


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z") if s else None


def libelle(name, overrides):
    if name in overrides:
        return overrides[name]
    clean = re.sub(r"^(SPK|K)\d+_+", "", name).replace("_", " ").strip()
    return clean or name


def main():
    if len(sys.argv) < 3 or not TOKEN:
        sys.exit("Usage : META_TOKEN=... update_dashboard.py <ad_account_id> <dossier> [client]")
    account = "act_" + sys.argv[1].replace("act_", "")
    folder = Path(sys.argv[2])
    client = sys.argv[3] if len(sys.argv) > 3 else ""
    cfg_path = folder / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    overrides = cfg.get("libelles", {})

    now = datetime.now(PARIS)
    today = now.date()

    campaigns = get(f"{account}/campaigns", fields="id,name,objective,effective_status,start_time,stop_time,"
                    "lifetime_budget,daily_budget", limit=200)
    recent = {r["campaign_id"] for r in get(f"{account}/insights", level="campaign", date_preset="last_3d",
                                             fields="campaign_id,spend", limit=500) if num(r.get("spend")) > 0}

    kept = []
    for c in campaigns:
        stop = parse_dt(c.get("stop_time"))
        start = parse_dt(c.get("start_time"))
        if start and start > now:
            continue
        future = stop is None or stop >= now
        ended_recently = stop is not None and now - timedelta(days=7) <= stop < now
        if c["id"] in recent or (future and c.get("effective_status") == "ACTIVE" and stop is not None) or ended_recently:
            kept.append(c)

    rows_c, rows_d, plat, rows_e = [], [], {}, []
    for c in kept:
        life = get(f"{c['id']}/insights", date_preset="maximum", fields=METRICS + ",frequency")
        if not life or num(life[0].get("spend")) <= 0:
            continue
        life = life[0]
        start = parse_dt(c.get("start_time"))
        stop = parse_dt(c.get("stop_time"))
        lab = libelle(c["name"], overrides)

        # Budget : budget campagne (CBO) sinon somme des ensembles (montants Meta en centimes)
        adsets = get(f"{c['id']}/adsets", fields="id,name,lifetime_budget,daily_budget,start_time,end_time", limit=200)

        def budget_of(o, s, e):
            if num(o.get("lifetime_budget")) > 0:
                return num(o["lifetime_budget"]) / 100
            if num(o.get("daily_budget")) > 0 and s and e:
                return num(o["daily_budget"]) / 100 * max(1, (e.date() - s.date()).days + 1)
            return 0.0

        budget = budget_of(c, start, stop) or sum(
            budget_of(a, parse_dt(a.get("start_time")), parse_dt(a.get("end_time"))) for a in adsets)

        statut = "En diffusion" if (stop is None or stop >= now) and c.get("effective_status") == "ACTIVE" else "Terminée"
        m = metrics(life)
        rows_c.append({
            "reseau": "Meta", "campaign_id": c["id"], "campagne": c["name"], "libelle": lab,
            "objectif": OBJECTIFS.get(c.get("objective"), c.get("objective", "")), "statut": statut,
            "debut": start.astimezone(PARIS).date().isoformat() if start else None,
            "fin": stop.astimezone(PARIS).date().isoformat() if stop else None,
            "budget": round(budget, 2), **m,
            "frequence": round(m["impressions"] / m["couverture"], 2) if m["couverture"] else None,
            "maj": now.isoformat(timespec="seconds"),
        })

        since = (start.astimezone(PARIS).date() if start else today - timedelta(days=30)).isoformat()
        for d in get(f"{c['id']}/insights", time_range=json.dumps({"since": since, "until": today.isoformat()}),
                     time_increment=1, fields=METRICS, limit=500):
            if num(d.get("spend")) <= 0 and num(d.get("impressions")) <= 0:
                continue
            rows_d.append({"date": d["date_start"], "reseau": "Meta", "campaign_id": c["id"], "libelle": lab, **metrics(d)})

        for p in get(f"{c['id']}/insights", date_preset="maximum", breakdowns="publisher_platform", fields=METRICS):
            name = PLATEFORMES.get(p.get("publisher_platform"), str(p.get("publisher_platform", "")).title())
            acc = plat.setdefault(name, {"plateforme": name, "reseau": "Meta", "depense": 0, "impressions": 0,
                                         "couverture": 0, "clics_lien": 0, "vues_video": 0, "engagements": 0})
            pm = metrics(p)
            for k in ("depense", "impressions", "couverture", "clics_lien", "vues_video", "engagements"):
                acc[k] += pm[k]

        ads = get(f"{c['id']}/ads", fields="name,adset_id", limit=500)
        ad_names = {}
        for a in ads:
            ad_names.setdefault(a["adset_id"], [])
            if a["name"] not in ad_names[a["adset_id"]]:
                ad_names[a["adset_id"]].append(a["name"])
        by_set = {a["id"]: a for a in adsets}
        for s in get(f"{c['id']}/insights", level="adset", date_preset="maximum",
                     fields="adset_id,adset_name,frequency," + METRICS, limit=500):
            sm = metrics(s)
            a = by_set.get(s["adset_id"], {})
            rows_e.append({
                "ensemble_id": s["adset_id"], "reseau": "Meta", "campaign_id": c["id"], "ensemble": s.get("adset_name"),
                "publicite": " / ".join(ad_names.get(s["adset_id"], [])),
                "budget": round(budget_of(a, parse_dt(a.get("start_time")), parse_dt(a.get("end_time"))), 2), **sm,
                "frequence": round(sm["impressions"] / sm["couverture"], 2) if sm["couverture"] else None,
            })

    for p in plat.values():
        p["depense"] = round(p["depense"], 2)

    data = {"client": client, "maj": now.isoformat(timespec="seconds"), "campagnes": rows_c,
            "quotidien": sorted(rows_d, key=lambda r: (r["date"], r["campaign_id"])),
            "plateformes": sorted(plat.values(), key=lambda r: -r["depense"]), "ensembles": rows_e}

    # Contrôle de cohérence avant d'écrire
    tot = sum(r["depense"] for r in rows_c)
    for name, rows in (("quotidien", rows_d), ("plateformes", list(plat.values())), ("ensembles", rows_e)):
        s = sum(r["depense"] for r in rows)
        if tot and abs(s - tot) / tot > 0.02:
            print(f"Attention : dépense {name} = {s:.2f} vs campagnes = {tot:.2f}")

    (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows_c)} campagne(s), {tot:.2f} € dépensés, maj {data['maj']}")


if __name__ == "__main__":
    main()
