import pandas as pd
import urllib.request


def main() -> None:
    df = pd.read_excel("marcas.xls")
    rows = []
    test_term = "m-160n"

    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]

    for _, record in df.iterrows():
        marca = str(record.get("marca") or "").strip()
        raw = str(record.get("url") or "").strip()
        raw = " ".join(raw.replace("\n", " ").replace("\r", " ").split())
        candidate = raw

        if not candidate or candidate.lower() in {"nan", "none"}:
            status, code, final, ok, note = "empty", "", "", False, "url vacia"
        elif "baudin" in candidate.lower() and "http" not in candidate.lower():
            candidate = "https://baudinequipamientos.com/tienda/?q={sku}"
            status, code, final, ok, note = (
                "template",
                "",
                "",
                True,
                "reemplazada por template baudin",
            )
        else:
            probe = candidate
            if "{sku}" in probe:
                probe = probe.replace("{sku}", test_term)
            elif probe.endswith("=") or probe.endswith("%3D"):
                probe = probe + test_term

            try:
                request = urllib.request.Request(probe)
                with opener.open(request, timeout=20) as response:
                    code = getattr(response, "status", 200)
                    final = response.geturl()
                ok = 200 <= int(code) < 400
                status = "ok" if ok else "error"
                note = "" if ok else f"status {code}"
            except Exception as exc:
                status, code, final, ok, note = "error", "", "", False, str(exc)

        rows.append(
            {
                "marca": marca,
                "url_original": raw,
                "url_sugerida": candidate,
                "status": status,
                "ok": ok,
                "http_status": code,
                "final_url": final,
                "nota": note,
            }
        )

    out = pd.DataFrame(rows)
    out.to_csv("marcas_url_check.csv", index=False, encoding="utf-8-sig")
    out.to_excel("marcas_url_check.xlsx", index=False)

    print(out[["marca", "status", "http_status", "url_sugerida"]].to_string(index=False))
    print("\nreportes: marcas_url_check.csv y marcas_url_check.xlsx")


if __name__ == "__main__":
    main()
