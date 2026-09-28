from __future__ import annotations
import pickle
import sys
from main import _process_one_pdf_page

def main(inp: str, out: str):
    with open(inp, 'rb') as f:
        payload = pickle.load(f)
    results=[]
    for page_bytes, native in payload:
        results.append(_process_one_pdf_page(page_bytes, native))
    with open(out, 'wb') as f:
        pickle.dump(results, f, protocol=pickle.HIGHEST_PROTOCOL)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
