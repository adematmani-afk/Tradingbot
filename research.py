"""Recherche de stratégie (simulation seulement, aucune alerte, aucun ordre).

Teste plusieurs familles de règles sur les 7 titres surveillés, avec deux jeux de données :
  - bougies 5 min sur ~60 jours (ce que la veille utilise en direct)
  - bougies 1 h sur ~2 ans (beaucoup plus de jours, donc des conclusions plus solides)

Règle d'honnêteté : on choisit sur la 1re moitié de l'historique et on juge sur la 2e.
Toutes les règles sont « achat seulement » (pas de vente à découvert dans un CELI),
intraday (sortie forcée 15 h 45), coût aller-retour déduit, une position à la fois par titre.

    python research.py            # rapport complet
"""
import json
import math
import os
import random
import sys
from datetime import time

import signals as S

TICKERS = S.CFG["tickers"]
COST = S.CFG["round_trip_cost_pct"]
FORCE_EXIT = time(15, 45)
NO_ENTRY_AFTER = time(15, 0)


def vwaps(bars):
    pv = vol = 0.0
    out = []
    for b in bars:
        pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        vol += b["v"]
        out.append(pv / vol if vol else b["c"])
    return out


def run_day(bars, prev_close, rule):
    """Rejoue une journée pour une règle. Retourne la liste des rendements nets (%)."""
    vw = vwaps(bars)
    day_open = bars[0]["o"]
    or_end = time(10, 0)
    or_high = max((b["h"] for b in bars if b["t"].time() < or_end), default=None)
    trades, pos, entries = [], None, 0
    armed = False
    for i, b in enumerate(bars):
        t = b["t"].time()
        if pos:
            if b["l"] <= pos["stop"]:
                trades.append((pos["stop"] / pos["entry"] - 1) * 100 - COST); pos = None
            elif pos["target"] and b["h"] >= pos["target"]:
                trades.append((pos["target"] / pos["entry"] - 1) * 100 - COST); pos = None
            elif t >= FORCE_EXIT or i == len(bars) - 1:
                trades.append((b["c"] / pos["entry"] - 1) * 100 - COST); pos = None
            continue
        if t >= NO_ENTRY_AFTER or entries >= rule.get("max_entries", 99):
            continue
        sig = rule["entry"](b, i, bars, vw, day_open, prev_close, or_high, t, rule, armed)
        if isinstance(sig, str) and sig == "arm":
            armed = True
            continue
        if sig:
            armed = False
            e = b["c"]
            stop = e * (1 - rule["stop"] / 100) if rule["stop"] else 0
            tgt = rule["target"](e, vw[i]) if rule.get("target") else None
            if tgt is not None and tgt <= e:
                tgt = e * 1.002  # cible déjà dépassée : on vise un petit gain
            pos = {"entry": e, "stop": stop, "target": tgt}
            entries += 1
    return trades


# ---------------------------------------------------------------- familles de règles

def e_reversion(b, i, bars, vw, o, pc, orh, t, r, armed):
    if r.get("after") and t < r["after"]:
        return False
    below = b["c"] <= vw[i] * (1 - r["dev"] / 100)
    if r.get("confirm"):
        # Attendre une bougie verte qui suit un passage sous le seuil.
        if armed and b["c"] > b["o"]:
            return True
        return "arm" if below else False
    return below


def e_orb(b, i, bars, vw, o, pc, orh, t, r, armed):
    return orh is not None and t >= time(10, 0) and b["c"] > orh and b["c"] > vw[i]


def e_trend(b, i, bars, vw, o, pc, orh, t, r, armed):
    return t >= r["at"] and b["c"] >= o * (1 + r["min"] / 100) and b["c"] > vw[i]


def e_gapfill(b, i, bars, vw, o, pc, orh, t, r, armed):
    return pc is not None and i == 0 and o <= pc * (1 - r["gap"] / 100)


def e_open(b, i, bars, vw, o, pc, orh, t, r, armed):
    return i == 0


def e_random(b, i, bars, vw, o, pc, orh, t, r, armed):
    return r["rng"].random() < r["p"]


to_vwap = lambda e, v: v
pct = lambda p: (lambda e, v: e * (1 + p / 100))


def rules():
    R = []
    for dev in (0.5, 1.0, 1.5, 2.0):
        for stop in (1.0, 2.0, 3.0, None):
            for tname, tgt in (("VWAP", to_vwap), ("+1%", pct(1.0)), ("fin", None)):
                for confirm in (False, True):
                    for after in (None, time(10, 30)):
                        name = f"reversion dev{dev} stop{stop or '-'} cible{tname}" + (" +bougie verte" if confirm else "") + (" après10h30" if after else "")
                        R.append({"name": name, "family": "reversion", "entry": e_reversion, "dev": dev, "stop": stop,
                                  "target": tgt, "confirm": confirm, "after": after})
    for stop in (1.0, 2.0, None):
        for tname, tgt in (("+3%", pct(3.0)), ("fin", None)):
            R.append({"name": f"cassure 1re demi-heure stop{stop or '-'} cible{tname}", "family": "momentum",
                      "entry": e_orb, "stop": stop, "target": tgt, "max_entries": 1})
    for at in (time(10, 30), time(11, 30)):
        for mn in (0.0, 0.5, 1.0):
            for stop in (1.0, 2.0, None):
                R.append({"name": f"journée haussière à {at:%H:%M} (+{mn}%) stop{stop or '-'} tenue jusqu'à 15h45",
                          "family": "tendance", "entry": e_trend, "at": at, "min": mn, "stop": stop, "target": None, "max_entries": 1})
    for gap in (0.5, 1.0, 2.0):
        for stop in (2.0, None):
            R.append({"name": f"achat après ouverture en baisse ≥{gap}% stop{stop or '-'} cible clôture veille",
                      "family": "écart", "entry": e_gapfill, "gap": gap, "stop": stop, "max_entries": 1,
                      "target": None, "_gap": True})
    R.append({"name": "repère : achat à l'ouverture, vente 15h45", "family": "repère", "entry": e_open, "stop": None, "target": None, "max_entries": 1})
    return R


def evaluate(data, rule):
    """data : {ticker: {date: bars}}. Retourne {ticker: (trades 1re moitié, trades 2e moitié)}."""
    res = {}
    for tk, days in data.items():
        keys = sorted(days)
        half = len(keys) // 2
        a, b = [], []
        prev = None
        for k, d in enumerate(keys):
            bars = days[d]
            r = dict(rule)
            if r.get("_gap") and prev is not None:
                r["target"] = (lambda pc: (lambda e, v: pc))(prev)
            if r["entry"] is e_random:
                r["rng"] = random.Random(hash((tk, str(d))) & 0xffff)
            tr = run_day(bars, prev, r)
            (a if k < half else b).extend(tr)
            prev = bars[-1]["c"]
        res[tk] = (a, b)
    return res


def compound(trs):
    eq = 100.0
    for x in trs:
        eq *= 1 + x / 100
    return eq


def summarize(res):
    A = [x for a, b in res.values() for x in a]
    B = [x for a, b in res.values() for x in b]
    mean = lambda v: sum(v) / len(v) if v else 0.0
    def tstat(v):
        if len(v) < 3:
            return 0.0
        m = mean(v); sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
        return m / (sd / math.sqrt(len(v))) if sd else 0.0
    both_pos = sum(1 for a, b in res.values() if a and b and mean(a) > 0 and mean(b) > 0)
    return {"nA": len(A), "nB": len(B), "mA": mean(A), "mB": mean(B), "tB": tstat(B),
            "winB": (sum(1 for x in B if x > 0) / len(B) * 100) if B else 0,
            "both_pos": both_pos, "eqB": {tk: compound(b) for tk, (a, b) in res.items()}}


def load(interval, range_):
    data = {}
    for tk in TICKERS:
        try:
            days = S.by_day(S.fetch_bars(tk, range_=range_, interval=interval))
        except Exception as ex:
            print(f"- {tk} {interval} : indisponible ({ex})")
            continue
        days = {d: v for d, v in days.items() if len(v) >= (3 if interval == "1h" else 30)}
        if len(days) >= 20:
            data[tk] = days
    return data


def random_baseline(data, n=200):
    """Distribution de l'espérance (2e moitié) pour des entrées au hasard, même sorties que la règle actuelle."""
    outs = []
    for seed in range(n):
        r = {"name": "hasard", "entry": e_random, "p": 0.02, "stop": 2.0, "target": None}
        res = {}
        for tk, days in data.items():
            keys = sorted(days); half = len(keys) // 2
            rng = random.Random(seed * 7919 + hash(tk) % 1000)
            b = []
            for d in keys[half:]:
                rr = dict(r); rr["rng"] = rng
                b.extend(run_day(days[d], None, rr))
            res[tk] = b
        allb = [x for v in res.values() for x in v]
        if allb:
            outs.append(sum(allb) / len(allb))
    outs.sort()
    return outs


def main():
    out = ["# Recherche de stratégie (simulation, achat seulement, intraday, coût %.2f %% déduit)" % COST]
    for label, interval, range_ in (("Bougies 1 h, ~2 ans", "1h", "730d"), ("Bougies 5 min, ~60 jours", "5m", "60d")):
        data = load(interval, range_)
        if not data:
            out.append(f"\n## {label}\n- données indisponibles")
            continue
        ndays = {tk: len(d) for tk, d in data.items()}
        out.append(f"\n## {label} — jours par titre : " + ", ".join(f"{k} {v}" for k, v in ndays.items()))
        rows = []
        for r in rules():
            s = summarize(evaluate(data, r))
            rows.append((r, s))
        base = next(s for r, s in rows if r["name"].startswith("reversion dev1.0 stop2.0 cibleVWAP") and "verte" not in r["name"] and "après" not in r["name"])
        out.append(f"- Règle actuelle (écart 1 %, stop 2 %, cible VWAP) : 1re moitié {base['mA']:+.3f} %/trade ({base['nA']}), "
                   f"2e moitié {base['mB']:+.3f} %/trade ({base['nB']}), t={base['tB']:+.1f}")
        # Choix sur la 1re moitié uniquement (au moins 30 trades), puis lecture de la 2e.
        cand = [(r, s) for r, s in rows if s["nA"] >= 30 and s["nB"] >= 30]
        cand.sort(key=lambda rs: rs[1]["mA"], reverse=True)
        out.append("\n### 10 meilleures règles choisies sur la 1re moitié, et ce qu'elles donnent ensuite")
        out.append("règle | 1re moitié %/trade (n) | 2e moitié %/trade (n) | t 2e moitié | réussite 2e | titres positifs sur les 2 moitiés")
        for r, s in cand[:10]:
            out.append(f"- {r['name']} | {s['mA']:+.3f} ({s['nA']}) | {s['mB']:+.3f} ({s['nB']}) | {s['tB']:+.1f} | {s['winB']:.0f} % | {s['both_pos']}/{len(data)}")
        posboth = [(r, s) for r, s in cand if s["mA"] > 0 and s["mB"] > 0]
        out.append(f"\n- Règles positives sur les deux moitiés : {len(posboth)} sur {len(cand)} testées")
        for fam in ("reversion", "momentum", "tendance", "écart", "repère"):
            fs = [s for r, s in cand if r["family"] == fam]
            if fs:
                out.append(f"- Famille {fam} : {len(fs)} règles, 2e moitié moyenne {sum(s['mB'] for s in fs) / len(fs):+.3f} %/trade, "
                           f"{sum(1 for s in fs if s['mA'] > 0 and s['mB'] > 0)} positives sur les deux moitiés")
        rb = random_baseline(data)
        if rb:
            out.append(f"- Entrées au hasard (stop 2 %, 200 tirages), 2e moitié : médiane {rb[len(rb) // 2]:+.3f} %/trade, "
                       f"5e–95e centile {rb[int(len(rb) * .05)]:+.3f} à {rb[int(len(rb) * .95)]:+.3f}")
        best = cand[0] if cand else None
        if best:
            out.append(f"- Comptes de 100 $ sur la 2e moitié avec la meilleure règle de la 1re moitié : "
                       + ", ".join(f"{tk} {v:.1f}" for tk, v in best[1]["eqB"].items()))
    report = "\n".join(out)
    print(report)
    os.makedirs(os.path.join(S.ROOT, "state"), exist_ok=True)
    open(os.path.join(S.ROOT, "state", "research.md"), "w").write(report + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(report + "\n")


if __name__ == "__main__":
    main()
