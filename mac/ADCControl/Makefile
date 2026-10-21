APP_NAME = ADCControlApp
BUILD_DIR = $(shell swift build --show-bin-path)
APP_BUNDLE = dist/ADC CRT Control.app
EXECUTABLE = $(BUILD_DIR)/$(APP_NAME)

.PHONY: build test app clean

build:
	swift build

test:
	swift run adcctl self-test

app:
	swift build --product $(APP_NAME)
	rm -rf "$(APP_BUNDLE)"
	mkdir -p "$(APP_BUNDLE)/Contents/MacOS"
	mkdir -p "$(APP_BUNDLE)/Contents/Resources"
	cp "$(EXECUTABLE)" "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	printf '%s\n' \
		'<?xml version="1.0" encoding="UTF-8"?>' \
		'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
		'<plist version="1.0">' \
		'<dict>' \
		'  <key>CFBundleExecutable</key>' \
		'  <string>$(APP_NAME)</string>' \
		'  <key>CFBundleIdentifier</key>' \
		'  <string>codes.roasted.adc-control</string>' \
		'  <key>CFBundleName</key>' \
		'  <string>ADC CRT Control</string>' \
		'  <key>CFBundleDisplayName</key>' \
		'  <string>ADC CRT Control</string>' \
		'  <key>CFBundlePackageType</key>' \
		'  <string>APPL</string>' \
		'  <key>CFBundleShortVersionString</key>' \
		'  <string>0.1.0</string>' \
		'  <key>LSMinimumSystemVersion</key>' \
		'  <string>13.0</string>' \
		'  <key>NSHighResolutionCapable</key>' \
		'  <true/>' \
		'</dict>' \
		'</plist>' \
		> "$(APP_BUNDLE)/Contents/Info.plist"
	codesign --force --sign - "$(APP_BUNDLE)"
	@echo "Built clickable app: $(APP_BUNDLE)"

clean:
	rm -rf .build dist
