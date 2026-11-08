SHELL := /bin/bash
.PHONY: test test-netns gui cli install uninstall package

test:
	python3 -m unittest discover -s tests -v

test-netns:
	./scripts/netns-integration.sh

gui:
	./cs2blocker-gui

cli:
	./cs2blockctl list

install:
	sudo ./packaging/install.sh

uninstall:
	sudo ./packaging/uninstall.sh --yes

package:
	cd packaging && makepkg -f
