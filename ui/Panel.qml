import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import qs.Commons
import qs.Ui

// Sift's search panel: type what you're looking for, results arrive as you type. Talks to the sift daemon over its
// Unix socket, one JSON object per line. Styled with the [menu] surface tokens, like the clipboard and launcher.
Item {
  id: root

  property bool opened: false
  property string query: ""
  property int selectedIndex: 0
  property int requestSeq: 0
  property int answeredSeq: 0
  property bool searching: requestSeq > answeredSeq
  property string error: ""
  property var status: ({})
  property bool showWeak: false
  property int weakCount: 0
  property var people: []
  property string view: "search"     // search | manage (Ctrl+S: folders and people)
  property bool needsSetup: false

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl("..")).replace(/^file:\/\//, ""))

  readonly property string sift: Quickshell.env("HOME") + "/.local/bin/sift"
  readonly property string socketPath: Quickshell.env("XDG_RUNTIME_DIR") + "/sift/sift.sock"

  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  property color selectedBackground: Color.menu.selectedBackground
  property color selectedText: Color.menu.selectedText
  readonly property int cornerRadius: Style.cornerRadius
  property string fontFamily: Style.font.menuFamily
  property int contentMargin: Style.spacing.panelPadding
  property int headerHeight: Math.max(Style.space(34), Style.font.title + Style.spacing.controlPaddingY * 2)
  property int footerHeight: Style.font.caption + Style.space(8)
  property int contentSpacing: Style.spacing.md
  property int cardWidth: Math.min(Style.space(960), panel.width - Style.gapsOut * 2)
  property int cardHeight: Math.min(Style.space(640), panel.height - Style.gapsOut * 2)
  property int rowHeight: Math.max(Style.space(58), Style.font.title + Style.font.caption + Style.space(26))

  // ------------------------------------------------------------ open / close

  // payloadJson may name a view: {"view": "folders"} or {"view": "people"} opens Ctrl+S's tabs directly.
  function open(payloadJson) {
    var payload = {}
    try { payload = JSON.parse(payloadJson || "{}") || {} } catch (e) {}
    root.opened = true
    root.query = ""
    root.error = ""
    root.selectedIndex = 0
    root.showWeak = false
    root.view = "search"
    results.clear()
    pointerGate.reset()
    sock.connected = true
    statusFile.reload()
    wrapper.reload()
    if (payload.view === "folders" || payload.view === "people") root.showManage(payload.view)
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function showManage(tab) {
    root.view = "manage"
    manage.opened(tab)
  }

  function searchFor(text) {
    root.view = "search"
    root.setQuery(text)
  }

  // First run from the plugin marketplace: Sift's files are here but nothing is built yet.
  function runSetup() {
    root.close()
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", Util.shellQuote(root.pluginDir + "/install.sh")])
  }

  function close() {
    root.opened = false
    debounce.stop()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open("{}")
  }

  // ------------------------------------------------------------ talking to the daemon

  function send(payload) {
    if (!sock.connected) {
      sock.connected = true
      return false
    }
    sock.write(JSON.stringify(payload) + "\n")
    sock.flush()
    return true
  }

  function runSearch() {
    if (!root.query.trim()) {
      results.clear()
      root.people = []
      root.answeredSeq = root.requestSeq
      return
    }
    root.requestSeq += 1
    // Not connected yet (the panel just opened, or the daemon restarted): the query stays pending, shown as
    // searching, and is sent again once the socket connects (onConnectedChanged).
    root.send({ id: root.requestSeq, op: "search", q: root.query, limit: 40 })
  }

  function handleReply(line) {
    var reply
    try { reply = JSON.parse(line) } catch (e) { return }
    if (reply.id === undefined || reply.id === null) return
    if (typeof reply.id === "string") {   // the Folders and People views' requests
      manage.handleReply(reply)
      return
    }
    if (reply.id < root.requestSeq) return   // an answer to an older query: a newer one is on its way
    root.answeredSeq = reply.id
    if (!reply.ok) {
      root.error = reply.error || "search failed"
      return
    }
    root.error = ""
    root.people = reply.people || []
    root.setResults(reply.results || [])
  }

  function setResults(hits) {
    results.clear()
    var weak = 0
    for (var i = 0; i < hits.length; i++) {
      var h = hits[i]
      if (!h.strong) weak++
      results.append({
        path: h.path,
        name: h.name,
        folder: h.folder,
        kind: h.kind,
        page: h.page || 0,
        snippet: h.snippet || "",
        strong: h.strong !== false,
        thumb: h.thumb || "",
        imgWidth: (h.info && h.info.width) || 0,
        imgHeight: (h.info && h.info.height) || 0,
        pages: (h.info && h.info.pages) || 0,
        mtime: h.mtime || 0
      })
    }
    root.weakCount = weak
    root.selectedIndex = 0
    Qt.callLater(function() { if (results.count > 0) resultList.positionViewAtIndex(0, ListView.Beginning) })
  }

  // ------------------------------------------------------------ actions

  function current() {
    return results.count > 0 && root.selectedIndex >= 0 && root.selectedIndex < results.count
      ? results.get(root.selectedIndex) : null
  }

  function openHit(hit) {
    if (!hit) return
    root.close()
    var args = [root.sift, "open", hit.path]
    if (hit.kind === "pdf" && hit.page > 0) args.push("--page", String(hit.page))
    Quickshell.execDetached(args)
  }

  function revealHit(hit) {
    if (!hit) return
    root.close()
    Quickshell.execDetached([root.sift, "reveal", hit.path])
  }

  function copyHit(hit) {
    if (!hit) return
    root.close()
    Quickshell.execDetached(["sh", "-c", "printf '%s\\n' \"$1\" | wl-copy --type text/uri-list", "sh", Util.fileUrl(hit.path)])
  }

  function select(delta) {
    if (results.count === 0) return
    pointerGate.reset()
    root.selectedIndex = Math.max(0, Math.min(results.count - 1, root.selectedIndex + delta))
    resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
  }

  function setQuery(text) {
    root.query = text
    root.showWeak = false
    debounce.restart()
  }

  // ------------------------------------------------------------ presentation helpers

  function kindIcon(kind) {
    if (kind === "pdf") return "󰈦"         // file-pdf-box
    if (kind === "screenshot") return "󰹑"  // monitor-screenshot
    if (kind === "image") return "󰋩"       // image
    return "󰈙"                              // file-document
  }

  function detail(hit) {
    var parts = [hit.folder]
    if (hit.kind === "pdf" && hit.page > 0) parts.push("page " + hit.page + (hit.pages ? " of " + hit.pages : ""))
    if ((hit.kind === "image" || hit.kind === "screenshot") && hit.imgWidth) parts.push(hit.imgWidth + "×" + hit.imgHeight)
    return parts.join("  ·  ")
  }

  function previewSource(hit) {
    if (!hit) return ""
    if (hit.kind === "image" || hit.kind === "screenshot") return Util.fileUrl(hit.path)
    if (hit.thumb) return Util.fileUrl(hit.thumb)
    return ""
  }

  function statusLine() {
    var s = root.status || {}
    var total = s.total || 0
    if (!total) return ""
    var n = function(v) { return Number(v || 0).toLocaleString(Qt.locale(), "f", 0) }
    if (s.state === "indexing" || s.state === "scanning")
      return "Reading " + n(s.done) + " of " + n(total) + (s.eta ? " · ~" + Math.max(1, Math.round(s.eta / 60)) + " min" : "")
    if (s.state === "faces") return "Looking for faces · " + n(s.done) + " of " + n(total) + " photos"
    if (s.state === "paused") return "Paused · " + n(s.done) + " of " + n(total)
    if (s.state === "battery") return "Waiting for the charger · " + n(s.done) + " of " + n(total)
    return n(total) + " files"
  }

  ListModel { id: results }

  PointerMoveGate {
    id: pointerGate
    referenceItem: card
  }

  Timer {
    id: debounce
    interval: 160
    repeat: false
    onTriggered: root.runSearch()
  }

  Socket {
    id: sock
    path: root.socketPath
    connected: false
    onConnectedChanged: {
      if (connected) {
        root.error = ""
        root.send({ op: "warm" })
        if (root.query.trim()) root.runSearch()
      } else if (root.opened) {
        root.error = "Sift isn't running. Start it with: systemctl --user start sift"
        reconnect.restart()
      }
    }
    onError: function(e) {
      if (root.opened) {
        root.error = "Sift isn't running. Start it with: systemctl --user start sift"
        reconnect.restart()
      }
    }
    parser: SplitParser {
      onRead: function(line) { root.handleReply(line) }
    }
  }

  Timer {
    id: reconnect
    interval: 1500
    repeat: false
    onTriggered: if (root.opened && !sock.connected) sock.connected = true
  }

  // The wrapper the installer writes: missing means setup hasn't run.
  FileView {
    id: wrapper
    path: root.sift
    printErrors: false
    onLoaded: root.needsSetup = false
    onLoadFailed: root.needsSetup = true
  }

  FileView {
    id: statusFile
    path: Quickshell.env("XDG_RUNTIME_DIR") + "/sift/status.json"
    printErrors: false
    onLoaded: {
      try { root.status = JSON.parse(text()) } catch (e) {}
    }
  }

  Timer {
    interval: 2000
    running: root.opened
    repeat: true
    onTriggered: statusFile.reload()
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-sift"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.close()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true

        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          var ctrl = event.modifiers & Qt.ControlModifier
          if (ctrl && event.key === Qt.Key_S && !root.needsSetup) {
            if (root.view === "manage") root.view = "search"
            else root.showManage("")
          } else if (root.view === "manage") {
            if (!manage.handleKey(event) && event.key === Qt.Key_Escape) root.view = "search"
          } else if (root.needsSetup && !sock.connected && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
            root.runSetup()
          } else if (event.key === Qt.Key_Escape) {
            if (root.query) root.setQuery("")
            else root.close()
          } else if (ctrl && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
            root.revealHit(root.current())
          } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            root.openHit(root.current())
          } else if (ctrl && event.key === Qt.Key_C) {
            root.copyHit(root.current())
          } else if (Util.editsFilter(event, root.query)) {
            root.setQuery(Util.editedFilter(event, root.query))
          } else if (event.key === Qt.Key_Up || (ctrl && event.key === Qt.Key_K) || (ctrl && event.key === Qt.Key_P)) {
            root.select(-1)
          } else if (event.key === Qt.Key_Down || (ctrl && event.key === Qt.Key_J) || (ctrl && event.key === Qt.Key_N)) {
            root.select(1)
          } else if (event.key === Qt.Key_PageUp) {
            root.select(-6)
          } else if (event.key === Qt.Key_PageDown) {
            root.select(6)
          } else if (event.key === Qt.Key_Tab && root.weakCount > 0) {
            root.showWeak = !root.showWeak
          } else if (!ctrl && event.text && event.text.length === 1 && event.text.charCodeAt(0) >= 32 && event.text.charCodeAt(0) !== 127) {
            root.setQuery(root.query + event.text)
          } else {
            return
          }
          event.accepted = true
        }
      }

      Column {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.contentSpacing

        // Query line, with how the index is doing on the right.
        Item {
          width: parent.width
          height: root.headerHeight
          visible: root.view === "search"

          Text {
            id: searchGlyph
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "󰍉"
            color: root.foreground
            opacity: root.searching ? 1 : 0.58
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading

            SequentialAnimation on opacity {
              running: root.searching
              loops: Animation.Infinite
              NumberAnimation { to: 0.3; duration: 380 }
              NumberAnimation { to: 1; duration: 380 }
            }
          }

          Text {
            textFormat: Text.PlainText
            anchors.left: searchGlyph.right
            anchors.leftMargin: Style.space(12)
            anchors.right: peopleChip.left
            anchors.rightMargin: Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            text: root.query || "Describe what you're looking for…"
            color: root.foreground
            opacity: root.query ? 1 : 0.58
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            elide: Text.ElideLeft
          }

          // The people the query named, recognized by face.
          Rectangle {
            id: peopleChip
            visible: root.people.length > 0
            anchors.right: statusText.left
            anchors.rightMargin: visible ? Style.space(12) : 0
            anchors.verticalCenter: parent.verticalCenter
            width: visible ? peopleLabel.implicitWidth + Style.space(16) : 0
            height: peopleLabel.implicitHeight + Style.space(8)
            radius: root.cornerRadius
            color: root.selectedBackground

            Text {
              id: peopleLabel
              anchors.centerIn: parent
              textFormat: Text.PlainText
              text: "\udb80\udc04  " + root.people.join(", ")
              color: root.selectedText
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.showManage("people")
            }
          }

          Text {
            id: statusText
            textFormat: Text.PlainText
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.statusLine()
            color: root.foreground
            opacity: 0.5
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Manage {
          id: manage
          panel: root
          width: parent.width
          height: parent.height - root.footerHeight - root.contentSpacing
          visible: root.view === "manage"
        }

        Item {
          width: parent.width
          height: parent.height - root.headerHeight - root.footerHeight - root.contentSpacing * 2
          visible: root.view === "search"

          Row {
            anchors.fill: parent
            visible: results.count > 0

            // Results.
            Item {
              width: Math.round(parent.width * 0.56)
              height: parent.height
              clip: true

              ListView {
                id: resultList
                anchors.fill: parent
                anchors.rightMargin: root.contentMargin
                model: results
                clip: true
                spacing: Style.space(4)
                boundsBehavior: Flickable.StopAtBounds

                delegate: Rectangle {
                  id: row
                  required property int index
                  required property string path
                  required property string name
                  required property string kind
                  required property string snippet
                  required property bool strong
                  required property string thumb
                  required property int page
                  required property string folder
                  required property int imgWidth
                  required property int imgHeight
                  required property int pages

                  readonly property bool hasCursor: index === root.selectedIndex
                  readonly property bool shown: strong || root.showWeak || index === 0
                  readonly property bool picture: kind === "image" || kind === "screenshot"

                  visible: shown
                  implicitWidth: ListView.view.width
                  implicitHeight: shown ? root.rowHeight : 0
                  radius: root.cornerRadius
                  color: hasCursor ? root.selectedBackground : "transparent"
                  opacity: strong ? 1 : 0.62

                  Item {
                    id: iconBox
                    anchors.left: parent.left
                    anchors.leftMargin: Style.space(10)
                    anchors.verticalCenter: parent.verticalCenter
                    width: root.rowHeight - Style.space(16)
                    height: width

                    Image {
                      id: thumbImage
                      anchors.fill: parent
                      visible: status === Image.Ready
                      source: row.picture ? Util.fileUrl(row.path) : (row.thumb ? Util.fileUrl(row.thumb) : "")
                      sourceSize.width: 128
                      sourceSize.height: 128
                      fillMode: Image.PreserveAspectCrop
                      asynchronous: true
                      smooth: true
                    }

                    Text {
                      anchors.centerIn: parent
                      visible: !thumbImage.visible
                      text: root.kindIcon(row.kind)
                      color: row.hasCursor ? root.selectedText : root.foreground
                      opacity: 0.8
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.heading
                    }
                  }

                  Column {
                    anchors.left: iconBox.right
                    anchors.leftMargin: Style.space(12)
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(12)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.space(3)

                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      text: row.name
                      color: row.hasCursor ? root.selectedText : root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.title
                      elide: Text.ElideMiddle
                    }

                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      text: root.detail(row)
                      color: row.hasCursor ? root.selectedText : root.foreground
                      opacity: 0.6
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideMiddle
                    }
                  }

                  MouseArea {
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onPositionChanged: function(mouse) {
                      if (pointerGate.moved(row, mouse)) root.selectedIndex = row.index
                    }
                    onClicked: {
                      root.selectedIndex = row.index
                      root.openHit(root.current())
                    }
                  }
                }

                footer: Item {
                  width: ListView.view ? ListView.view.width : 0
                  height: root.weakCount > 0 && !root.showWeak ? Style.space(36) : 0
                  visible: height > 0

                  Text {
                    anchors.centerIn: parent
                    textFormat: Text.PlainText
                    text: root.weakCount + " weaker match" + (root.weakCount === 1 ? "" : "es") + " · Tab to show"
                    color: root.foreground
                    opacity: 0.5
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }

            // Preview of the selected result.
            Item {
              id: preview
              width: parent.width - Math.round(parent.width * 0.56)
              height: parent.height
              clip: true

              property var hit: results.count > 0 && root.selectedIndex < results.count ? results.get(root.selectedIndex) : null

              Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: Style.normalBorderWidth
                color: Util.alpha(root.border, 0.28)
              }

              Column {
                anchors.fill: parent
                anchors.leftMargin: root.contentMargin
                spacing: Style.space(10)

                Image {
                  id: previewImage
                  width: parent.width
                  height: visible ? Math.min(parent.height * 0.62, implicitHeight > 0 ? parent.width * implicitHeight / Math.max(implicitWidth, 1) : parent.height * 0.62) : 0
                  visible: source !== "" && status !== Image.Error
                  source: root.previewSource(preview.hit)
                  sourceSize.width: 900
                  fillMode: Image.PreserveAspectFit
                  horizontalAlignment: Image.AlignLeft
                  verticalAlignment: Image.AlignTop
                  asynchronous: true
                  smooth: true
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  text: preview.hit ? preview.hit.name : ""
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  wrapMode: Text.WrapAnywhere
                  maximumLineCount: 2
                  elide: Text.ElideRight
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  text: preview.hit ? root.detail(preview.hit) : ""
                  color: root.foreground
                  opacity: 0.6
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WrapAnywhere
                }

                // The passage that matched, with the query's words marked («…» from the index) in the accent.
                Text {
                  width: parent.width
                  height: parent.height - y
                  visible: preview.hit && preview.hit.snippet !== ""
                  textFormat: Text.StyledText
                  text: preview.hit ? root.markSnippet(preview.hit.snippet) : ""
                  color: root.foreground
                  opacity: 0.85
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  wrapMode: Text.Wrap
                  elide: Text.ElideRight
                  verticalAlignment: Text.AlignTop
                }
              }
            }
          }

          // Empty and error states.
          Column {
            anchors.centerIn: parent
            width: parent.width * 0.8
            spacing: Style.space(8)
            visible: results.count === 0

            Text {
              text: root.error && !root.needsSetup ? "" : "󰍉"
              color: root.selectedText
              opacity: 0.8
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
              horizontalAlignment: Text.AlignHCenter
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.needsSetup && root.error ? "Sift needs a one-time setup"
                : root.error ? root.error
                : !root.query ? "Photos, screenshots, PDFs and documents, by what's in them"
                : root.searching ? "Searching…"
                : "Nothing matches “" + root.query + "”"
              color: root.foreground
              opacity: 0.7
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              width: parent.width
            }

            Text {
              visible: root.needsSetup && root.error !== ""
              textFormat: Text.PlainText
              text: "It builds its search engine for this machine (about 3 minutes) and downloads the model "
                + "(865 MB), then asks which folders to read. Everything stays on this computer.\n\n↵  Set up Sift"
              color: root.foreground
              opacity: 0.65
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              width: parent.width
            }

            Text {
              visible: !root.query && !root.error
              textFormat: Text.PlainText
              text: "Try \u201cscreenshot of an error\u201d, \u201cthe lease pdf about pets\u201d, \u201cINV-2041\u201d"
              color: root.foreground
              opacity: 0.45
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              width: parent.width
            }
          }
        }

        Text {
          width: parent.width
          height: root.footerHeight
          textFormat: Text.PlainText
          text: root.view === "manage" ? manage.hints
            : root.needsSetup && root.error ? "↵ set up   esc close"
            : "↵ open   ctrl ↵ show in folder   ctrl c copy file   ctrl s folders & people   esc clear"
          color: root.foreground
          opacity: 0.4
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignRight
          verticalAlignment: Text.AlignVCenter
        }
      }
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  function markSnippet(snippet) {
    var accent = Color.accent
    return root.escapeHtml(snippet)
      .replace(/«/g, "<b><font color=\"" + accent + "\">")
      .replace(/»/g, "</font></b>")
  }
}
