"""Isolated offline Chinese/English OCR with a parent-enforced time budget."""
import contextlib
import json
import sys


def main():
    # Keep library diagnostics out of the machine-readable result.
    with contextlib.redirect_stdout(sys.stderr):
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
        result, _ = engine(sys.argv[1])
    rows = []
    uncertain = False
    for box, text, confidence in result or []:
        if confidence < 0.65:
            uncertain = True
            continue
        top = min(point[1] for point in box)
        bottom = max(point[1] for point in box)
        rows.append((sum(point[0] for point in box)/4, (top+bottom)/2,
                     max(1,bottom-top), text))
    lines = []
    for x, y, height, text in sorted(rows,key=lambda row:(row[1],row[0])):
        if lines and abs(y-lines[-1][0]) <= max(height,lines[-1][1])*0.5:
            lines[-1][2].append((x,text))
        else:
            lines.append([y,height,[(x,text)]])
    raw = "\n".join(" ".join(text for _,text in sorted(parts)) for _,_,parts in lines)
    note = "部分低置信度文字未采用，请对照原件补全。" if uncertain else ""
    print(json.dumps({"text":raw,"note":note},ensure_ascii=False))


if __name__ == '__main__': main()
