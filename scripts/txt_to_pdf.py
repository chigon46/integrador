from pathlib import Path

from fpdf import FPDF


def soft_wrap_long_tokens(text: str, max_token_len: int = 70) -> str:
    parts = text.split(" ")
    wrapped: list[str] = []
    for part in parts:
        if len(part) <= max_token_len:
            wrapped.append(part)
            continue
        chunks = [part[i : i + max_token_len] for i in range(0, len(part), max_token_len)]
        wrapped.append(" ".join(chunks))
    return " ".join(wrapped)


def txt_to_pdf(txt_path: Path, pdf_path: Path) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    with txt_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if not line.strip():
                pdf.ln(4)
                continue
            pdf.multi_cell(0, 6, soft_wrap_long_tokens(line), new_x="LMARGIN", new_y="NEXT")

    pdf.output(str(pdf_path))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    txt_file = root / "INSTRUCCIONES_OPERATIVAS_ACCIONES.txt"
    pdf_file = root / "INSTRUCCIONES_OPERATIVAS_ACCIONES.pdf"
    txt_to_pdf(txt_file, pdf_file)
    print(f"PDF generado: {pdf_file}")
