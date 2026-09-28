from __future__ import annotations

import pickle
import sys

from document_segmentation import _analyze_one_page_from_pdf


def main(inp: str, out: str) -> None:
    with open(inp, "rb") as f:
        pdf_path, native_text, index, dpi = pickle.load(f)
    result = _analyze_one_page_from_pdf(pdf_path, native_text, index, dpi)
    with open(out, "wb") as f:
        pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
