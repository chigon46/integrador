from pathlib import Path

from fpdf import FPDF


def _soft_wrap_long_tokens(text: str, max_token_len: int = 70) -> str:
    parts = text.split(" ")
    wrapped: list[str] = []
    for part in parts:
        if len(part) <= max_token_len:
            wrapped.append(part)
            continue

        chunks = [part[i : i + max_token_len] for i in range(0, len(part), max_token_len)]
        wrapped.append(" ".join(chunks))
    return " ".join(wrapped)


def clean_markdown_line(line: str) -> str:
    text = line.rstrip("\n")
    replacements = {
        "# ": "",
        "## ": "",
        "### ": "",
        "#### ": "",
        "- ": "- ",
        "* ": "- ",
        "```": "",
        "`": "",
    }
    for src, dst in replacements.items():
        if text.startswith(src):
            text = dst + text[len(src) :]
            break
    return text


def md_to_pdf(md_path: Path, pdf_path: Path) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    with md_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = clean_markdown_line(raw_line)
            if not line.strip():
                pdf.ln(4)
                continue

            pdf.multi_cell(0, 6, _soft_wrap_long_tokens(line), new_x="LMARGIN", new_y="NEXT")

    pdf.output(str(pdf_path))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    md_file = root / "DOCUMENTACION_OPERATIVA_SOVAI.md"
    pdf_file = root / "DOCUMENTACION_OPERATIVA_SOVAI.pdf"
    md_to_pdf(md_file, pdf_file)
    print(f"PDF generado: {pdf_file}")
