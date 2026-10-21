# adc-crt-control (Linux): install, packaging and checks.
# Run from this directory, or from the repository root with `make -C linux ...`.
#
#   make install [DESTDIR=...] [PREFIX=/usr]  install (as root), or stage into DESTDIR for packaging
#   make uninstall [DESTDIR=...] [PREFIX=/usr]
#   make deb        build dist/adc-crt-control_$(VERSION)_all.deb (Raspberry Pi OS / Debian)
#   make test       unit tests (no hardware needed)
#   make check      syntax checks, .desktop validation, version consistency
#   make clean
#
# Arch / CachyOS: packaging/arch/PKGBUILD calls `make install` (see README.md).

PACKAGE := adc-crt-control
# The one place the version is defined. `make deb` writes it into DEBIAN/control, and
# `make check` / `make deb` fail if packaging/debian/control, packaging/arch/PKGBUILD or
# adc_crt/__init__.py disagree with it.
VERSION := 3.0.0

PREFIX ?= /usr
DESTDIR ?=
PYTHON ?= python3

BINDIR = $(PREFIX)/bin
LIBDIR = $(PREFIX)/lib/$(PACKAGE)
APPLICATIONSDIR = $(PREFIX)/share/applications
# Fixed system locations, independent of PREFIX: udev, systemd, modules-load and XDG autostart
# only look here.
UDEVRULESDIR = /usr/lib/udev/rules.d
SYSTEMDUNITDIR = /usr/lib/systemd/system
MODULESLOADDIR = /usr/lib/modules-load.d
AUTOSTARTDIR = /etc/xdg/autostart

# Whatever modules exist when make runs (globbed, so new modules need no Makefile change).
PY_FILES = $(sort $(wildcard adc_crt/*.py))
TEST_FILES = $(sort $(wildcard tests/*.py))
SCRIPTS = bin/adc-crt data/libexec/cleanup-old-labwc-keybind
DESKTOP_FILES = data/applications/adc-crt-control.desktop data/autostart/adc-crt-buttons.desktop
MAINTSCRIPTS = postinst prerm postrm

DEBROOT = build/deb-root
DEB = dist/$(PACKAGE)_$(VERSION)_all.deb

.PHONY: all install uninstall deb test check version-check clean

all: check

install:
	@if [ -z "$(PY_FILES)" ]; then echo "error: no adc_crt/*.py files to install" >&2; exit 1; fi
	install -d -m 0755 "$(DESTDIR)$(LIBDIR)/adc_crt" "$(DESTDIR)$(BINDIR)" \
		"$(DESTDIR)$(UDEVRULESDIR)" "$(DESTDIR)$(SYSTEMDUNITDIR)" "$(DESTDIR)$(MODULESLOADDIR)" \
		"$(DESTDIR)$(APPLICATIONSDIR)" "$(DESTDIR)$(AUTOSTARTDIR)"
	install -m 0644 $(PY_FILES) "$(DESTDIR)$(LIBDIR)/adc_crt/"
	install -m 0755 data/libexec/cleanup-old-labwc-keybind "$(DESTDIR)$(LIBDIR)/cleanup-old-labwc-keybind"
	sed 's|@LIBDIR@|$(LIBDIR)|' bin/adc-crt > "$(DESTDIR)$(BINDIR)/adc-crt"
	chmod 0755 "$(DESTDIR)$(BINDIR)/adc-crt"
	@if grep -q '@LIBDIR@' "$(DESTDIR)$(BINDIR)/adc-crt"; then \
		echo "error: @LIBDIR@ was not substituted in $(DESTDIR)$(BINDIR)/adc-crt" >&2; exit 1; fi
	install -m 0644 data/udev/70-adc-crt-control.rules "$(DESTDIR)$(UDEVRULESDIR)/70-adc-crt-control.rules"
	sed 's|^ExecStart=/usr/bin/adc-crt |ExecStart=$(BINDIR)/adc-crt |' data/systemd/adc-crt-poweron.service \
		> "$(DESTDIR)$(SYSTEMDUNITDIR)/adc-crt-poweron.service"
	chmod 0644 "$(DESTDIR)$(SYSTEMDUNITDIR)/adc-crt-poweron.service"
	install -m 0644 data/modules-load/adc-crt-control.conf "$(DESTDIR)$(MODULESLOADDIR)/adc-crt-control.conf"
	install -m 0644 data/applications/adc-crt-control.desktop "$(DESTDIR)$(APPLICATIONSDIR)/adc-crt-control.desktop"
	install -m 0644 data/autostart/adc-crt-buttons.desktop "$(DESTDIR)$(AUTOSTARTDIR)/adc-crt-buttons.desktop"

uninstall:
	@if [ -z "$(DESTDIR)" ] && [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then \
		systemctl disable adc-crt-poweron.service 2>/dev/null || true; fi
	rm -f "$(DESTDIR)$(BINDIR)/adc-crt" \
		"$(DESTDIR)$(UDEVRULESDIR)/70-adc-crt-control.rules" \
		"$(DESTDIR)$(SYSTEMDUNITDIR)/adc-crt-poweron.service" \
		"$(DESTDIR)$(MODULESLOADDIR)/adc-crt-control.conf" \
		"$(DESTDIR)$(APPLICATIONSDIR)/adc-crt-control.desktop" \
		"$(DESTDIR)$(AUTOSTARTDIR)/adc-crt-buttons.desktop"
	rm -rf "$(DESTDIR)$(LIBDIR)"

deb: version-check
	rm -rf "$(DEBROOT)"
	$(MAKE) --no-print-directory install DESTDIR="$(CURDIR)/$(DEBROOT)" PREFIX=/usr
	install -d -m 0755 "$(DEBROOT)/DEBIAN" "$(DEBROOT)/usr/share/doc/$(PACKAGE)"
	install -m 0644 ../LICENSE "$(DEBROOT)/usr/share/doc/$(PACKAGE)/copyright"
	for s in $(MAINTSCRIPTS); do install -m 0755 "packaging/debian/$$s" "$(DEBROOT)/DEBIAN/$$s" || exit 1; done
	# Parent directories created by `install -d` follow the umask; ship them as 0755.
	find "$(DEBROOT)" -type d -exec chmod 0755 {} +
	# Everything shipped under /etc is a conffile (Debian policy 10.7.3).
	cd "$(DEBROOT)" && find etc -type f | LC_ALL=C sort | sed 's|^|/|' > DEBIAN/conffiles
	size=$$(du -sk --apparent-size --exclude=DEBIAN "$(DEBROOT)" | cut -f1) && \
	sed -e 's/^Version:.*/Version: $(VERSION)/' -e "/^Description:/i Installed-Size: $$size" \
		packaging/debian/control > "$(DEBROOT)/DEBIAN/control"
	cd "$(DEBROOT)" && find . -path ./DEBIAN -prune -o -type f -print | sed 's|^\./||' \
		| LC_ALL=C sort | xargs md5sum > DEBIAN/md5sums
	chmod 0644 "$(DEBROOT)/DEBIAN/control" "$(DEBROOT)/DEBIAN/conffiles" "$(DEBROOT)/DEBIAN/md5sums"
	install -d dist
	dpkg-deb --root-owner-group --build "$(DEBROOT)" "$(DEB)"
	@echo "Built $(DEB). Install with: sudo apt install ./$(DEB)"

test:
	$(PYTHON) -m unittest discover -s tests -t .

check: version-check
	$(PYTHON) -B -c 'import sys; [compile(open(p, "rb").read(), p, "exec") for p in sys.argv[1:]]' \
		$(PY_FILES) $(TEST_FILES) $(SCRIPTS)
	for s in $(MAINTSCRIPTS); do sh -n "packaging/debian/$$s" || exit 1; done
	bash -n packaging/arch/PKGBUILD
	bash -n packaging/arch/adc-crt-control.install
	@if command -v desktop-file-validate >/dev/null 2>&1; then \
		echo "desktop-file-validate $(DESKTOP_FILES)"; desktop-file-validate $(DESKTOP_FILES); \
	else echo "desktop-file-validate not installed; skipping .desktop validation"; fi
	@echo "check: OK"

version-check:
	@ctl=$$(sed -n 's/^Version: *//p' packaging/debian/control); \
	pkg=$$(sed -n 's/^pkgver=//p' packaging/arch/PKGBUILD); \
	py=$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' adc_crt/__init__.py); \
	for v in "$$ctl" "$$pkg" "$$py"; do \
		if [ "$$v" != "$(VERSION)" ]; then \
			echo "error: version mismatch: Makefile $(VERSION), packaging/debian/control '$$ctl'," \
				"packaging/arch/PKGBUILD '$$pkg', adc_crt/__init__.py '$$py'" >&2; exit 1; fi; \
	done

clean:
	rm -rf build dist packaging/arch/src packaging/arch/pkg packaging/arch/*.pkg.tar.*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
