# Standard Qt translation workflow.  Run `make` after changing strings in
# ../gui.py, then translate the new entries in deskcage_zh_CN.ts with Qt Linguist
# (or any XML editor) and run `make` again to rebuild the .qm.

PYLUPDATE ?= pylupdate6
LRELEASE ?= lrelease6
PYTHON ?= python3
SOURCE = ../gui.py
TS = deskcage_zh_CN.ts
QM = deskcage_zh_CN.qm

all: $(QM)

# lupdate 6 writes a flat, hard-to-diff catalogue; normalize.py re-indents it so
# only the strings that really changed show up in the diff.
$(TS): $(SOURCE) normalize.py
	$(PYLUPDATE) --no-obsolete $(SOURCE) -ts $(TS)
	$(PYTHON) normalize.py $(TS)

$(QM): $(TS)
	$(LRELEASE) $(TS) -qm $(QM)

clean:
	rm -f $(QM)

.PHONY: all clean
