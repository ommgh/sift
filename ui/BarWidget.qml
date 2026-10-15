import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Shows up only while Sift has something to say: indexing, paused, or failing.
BarWidget {
  id: root
  moduleName: "sift"

  property var status: ({ state: "stopped" })
  readonly property string state: status.state || "stopped"
  readonly property bool busy: state === "indexing" || state === "scanning" || state === "starting"
  readonly property string sift: Quickshell.env("HOME") + "/.local/bin/sift"

  function describe() {
    var s = root.status
    var count = (s.done || 0).toLocaleString(Qt.locale(), "f", 0) + " of " + (s.total || 0).toLocaleString(Qt.locale(), "f", 0) + " files"
    var line
    if (root.state === "indexing") {
      line = "Sift: reading " + count
      if (s.eta) line += " · ~" + Math.max(1, Math.round(s.eta / 60)) + " min left"
    } else if (root.state === "scanning") line = "Sift: looking for new files"
    else if (root.state === "paused") line = "Sift: paused at " + count + " · right-click to resume"
    else if (root.state === "battery") line = "Sift: waits for the charger · " + count
    else if (root.state === "error") line = "Sift: " + (s.current || "error")
    else line = "Sift: " + count
    return line
  }

  visible: busy || state === "paused" || state === "error"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  FileView {
    id: statusFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/sift/status.json"
    watchChanges: true
    printErrors: false
    onLoaded: {
      try { root.status = JSON.parse(text()) } catch (e) {}
    }
    onFileChanged: reload()
    onLoadFailed: root.status = { state: "stopped" }
  }

  // The status file is replaced, not rewritten, so a watch can miss it; a slow poll catches up.
  Timer {
    interval: 4000
    running: true
    repeat: true
    onTriggered: statusFile.reload()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰍉"
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    dimmed: root.state === "paused" || root.state === "battery"
    active: root.state === "error"
    tooltipText: root.describe()
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton) Quickshell.execDetached([root.sift, "toggle"])
      else if (root.bar) root.bar.run("omarchy-shell shell toggle sift '{}'")
    }
  }
}
