#!/bin/bash
set -e
/usr/bin/osascript -e 'display dialog "Remove Android Explorer from /Applications?" with title "Uninstall Android Explorer" buttons {"Cancel","Remove"} default button "Remove" cancel button "Cancel" with icon caution' >/dev/null || exit 0
sudo rm -rf "/Applications/Android Explorer.app"
/usr/bin/osascript -e 'display dialog "Android Explorer has been removed." with title "Uninstall Android Explorer" buttons {"OK"} default button "OK"'
