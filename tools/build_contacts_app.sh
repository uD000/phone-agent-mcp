#!/bin/sh
# Builds bin/PhoneAgentContacts.app: a tiny ad-hoc-signed app that reads your Mac's Contacts (read-only). macOS only lets an
# APP bundle (not a bare command-line tool) show the one-time Contacts permission prompt. Optional: only needed for
# the search_contacts / get_contact_numbers tools.
set -eu
cd "$(dirname "$0")/.."
command -v swiftc >/dev/null 2>&1 || { echo "swiftc not found (install Xcode)"; exit 1; }
APP=bin/PhoneAgentContacts.app
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.phone-agent.contacts</string>
<key>CFBundleName</key><string>PhoneAgentContacts</string>
<key>CFBundleExecutable</key><string>PhoneAgentContacts</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string><key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSUIElement</key><true/>
<key>NSContactsUsageDescription</key><string>phone-agent reads your contact names and numbers (read-only) so an AI agent can pick the right recipient and ask you when a name does not match exactly. Nothing is stored or sent anywhere.</string>
</dict></plist>
PLIST
swiftc -O tools/contacts.swift -o "$APP/Contents/MacOS/PhoneAgentContacts"
codesign --force --sign - "$APP"
echo "built $APP (macOS asks for Contacts permission the first time it runs)"
