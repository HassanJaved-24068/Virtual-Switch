#!/bin/bash
# Builds report/Report.pdf from report/Report.ms (groff + pic + tbl, then ps2pdf). Needs groff, ghostscript.
cd "$(dirname "$0")" || exit 1
groff -p -t -Tps Report.ms > Report.ps && ps2pdf -dPDFSETTINGS=/prepress Report.ps Report.pdf && rm -f Report.ps && pdfinfo Report.pdf | grep -E "^Pages"
