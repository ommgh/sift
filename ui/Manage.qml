import QtQuick
import QtQuick.Effects
import qs.Commons
import qs.Ui

// Ctrl+S in the panel: the folders Sift reads and the people it knows. Talks to the daemon through the panel's
// socket (panel.send), with ids starting "m" so the panel hands the replies here.
Item {
  id: root

  required property var panel
  property string tab: "folders"   // folders | people
  property int seq: 0
  property string notice: ""        // one line under the tabs: what just happened, or what a key will do
  property bool working: false

  // Folders
  property var folders: []
  property var suggest: []
  property int folderCursor: 0
  property bool browsing: false
  property var browse: ({ path: "", label: "", parent: null, dirs: [] })
  property int browseCursor: 0
  property int armedFolder: -1      // Delete pressed once on this row: press again to remove

  // People
  property var people: ({ enabled: false, named: [], groups: [] })
  property int personCursor: 0
  property bool naming: false
  property string nameDraft: ""
  property int armedPerson: -1
  property bool armedOff: false

  readonly property var folderRows: {
    var rows = []
    for (var i = 0; i < root.folders.length; i++) rows.push({ type: "folder", item: root.folders[i] })
    rows.push({ type: "add" })
    for (var j = 0; j < root.suggest.length; j++) rows.push({ type: "suggest", item: root.suggest[j] })
    return rows
  }
  readonly property var browseRows: {
    // Not at home itself: a stray Enter shouldn't add all of it.
    var rows = root.browse.label === "~" ? [] : [{ type: "here" }]
    for (var i = 0; i < root.browse.dirs.length; i++) rows.push({ type: "dir", item: root.browse.dirs[i] })
    return rows
  }
  readonly property var personRows: {
    var rows = []
    var named = root.people.named || []
    var groups = root.people.groups || []
    for (var i = 0; i < named.length; i++) rows.push({ type: "named", item: named[i] })
    for (var j = 0; j < groups.length; j++) rows.push({ type: "group", item: groups[j] })
    return rows
  }

  readonly property string hints: {
    if (root.tab === "folders") {
      if (root.browsing) return "↵ open   space add   ← up   esc back"
      return "space subfolders on/off   del remove   tab people   esc search"
    }
    if (!root.people.enabled) return "↵ turn on   tab folders   esc search"
    if (root.naming) return "↵ save   esc cancel"
    return "type a name   ↵ search them   del forget/hide   tab folders   esc search"
  }

  // ------------------------------------------------------------ daemon

  function request(payload) {
    root.seq += 1
    payload.id = "m" + root.seq
    if (!root.panel.send(payload)) root.notice = "Connecting to Sift…"
  }

  function refresh() {
    root.request({ op: root.tab === "folders" ? "folders" : "people" })
  }

  function opened(tab) {
    root.tab = tab || root.tab
    root.browsing = false
    root.naming = false
    root.notice = ""
    root.disarm()
    root.request({ op: "folders" })
    root.request({ op: "people" })
  }

  function handleReply(reply) {
    root.working = false
    if (!reply.ok) {
      root.notice = reply.error || "That didn't work"
      return
    }
    if (reply.folders !== undefined) {
      root.folders = reply.folders
      root.suggest = reply.suggest || []
      root.folderCursor = Math.min(root.folderCursor, root.folderRows.length - 1)
    }
    if (reply.browse !== undefined) {
      root.browse = reply.browse
      root.browseCursor = 0
      root.browsing = true
    }
    if (reply.people !== undefined && !root.naming) {
      root.people = reply.people
      root.personCursor = Math.max(0, Math.min(root.personCursor, root.personRows.length - 1))
    }
  }

  Timer {
    interval: 3000
    repeat: true
    running: root.visible && !root.naming && !root.browsing
    onTriggered: root.refresh()
  }

  // ------------------------------------------------------------ actions

  function disarm() {
    root.armedFolder = -1
    root.armedPerson = -1
    root.armedOff = false
  }

  function count(n) {
    return Number(n || 0).toLocaleString(Qt.locale(), "f", 0)
  }

  function folderDetail(f) {
    if (!f.exists) return "not found"
    var read = f.files === 0 ? "nothing read yet"
      : f.done >= f.files ? root.count(f.files) + " files"
      : root.count(f.done) + " of " + root.count(f.files) + " files read"
    return read + "  ·  " + (f.subfolders ? "with subfolders" : "only the files directly in it")
  }

  function activateFolderRow(index) {
    var row = root.folderRows[index]
    if (!row) return
    if (row.type === "add") root.request({ op: "browse", path: "~" })
    else if (row.type === "suggest") {
      root.request({ op: "folder-add", path: row.item.path, subfolders: true })
      root.notice = "Added " + row.item.label + ". Sift starts reading it in a moment."
    } else root.toggleSubfolders(index)
  }

  function toggleSubfolders(index) {
    var row = root.folderRows[index]
    if (!row || row.type !== "folder") return
    root.request({ op: "folder-subfolders", path: row.item.path, on: !row.item.subfolders })
    root.notice = row.item.label + (row.item.subfolders ? ": only the files directly in it" : ": subfolders too")
  }

  function removeFolder(index) {
    var row = root.folderRows[index]
    if (!row || row.type !== "folder") return
    if (root.armedFolder !== index) {
      root.armedFolder = index
      root.notice = "Press Delete again to stop reading " + row.item.label + " (its files leave the index; the files stay)"
      return
    }
    root.disarm()
    root.request({ op: "folder-remove", path: row.item.path })
    root.notice = "Removed " + row.item.label
  }

  function activateBrowseRow(index) {
    var row = root.browseRows[index]
    if (!row) return
    if (row.type === "here") root.addBrowsed(root.browse.path, root.browse.label, root.browse.added || root.browse.covered)
    else root.request({ op: "browse", path: row.item.path })
  }

  function addBrowsed(path, label, already) {
    if (already) {
      root.notice = label + " is already read"
      return
    }
    root.request({ op: "folder-add", path: path, subfolders: true })
    root.browsing = false
    root.notice = "Added " + label + ". Sift starts reading it in a moment."
  }

  function activatePerson(index) {
    var row = root.personRows[index]
    if (!row) return
    if (row.type === "named") root.panel.searchFor(row.item.name)
    else root.startNaming("")
  }

  function startNaming(first) {
    var row = root.personRows[root.personCursor]
    if (!row) return
    root.disarm()
    root.naming = true
    root.nameDraft = first
  }

  function saveName() {
    var row = root.personRows[root.personCursor]
    var name = root.nameDraft.trim()
    root.naming = false
    if (!row || !name) return
    root.working = true
    if (row.type === "named") {
      if (name === row.item.name) return
      root.request({ op: "person-rename", name: row.item.name, to: name })
      root.notice = "Renamed to " + name
    } else {
      root.request({ op: "person-name", face: row.item.id, name: name })
      root.notice = "Named " + name + ". Try searching “" + name + " outdoors”."
    }
  }

  function forgetPerson(index) {
    var row = root.personRows[index]
    if (!row) return
    if (root.armedPerson !== index) {
      root.armedPerson = index
      root.notice = row.type === "named"
        ? "Press Delete again to forget " + row.item.name + " (their photos stay)"
        : "Press Delete again to hide these faces (strangers, posters): they won't be suggested again"
      return
    }
    root.disarm()
    if (row.type === "named") root.request({ op: "person-remove", name: row.item.name })
    else root.request({ op: "group-hide", face: row.item.id })
    root.notice = row.type === "named" ? "Forgot " + row.item.name : "Hidden"
  }

  function setFaces(on) {
    if (!on && !root.armedOff) {
      root.armedOff = true
      root.notice = "Press Ctrl+Delete again to turn people off and delete every faceprint and name"
      return
    }
    root.disarm()
    root.working = true
    root.notice = on ? "Turning on: downloading two small face models (37 MB)…" : "People are off; face data deleted"
    root.request({ op: "faces", on: on })
  }

  // ------------------------------------------------------------ keys (the panel forwards them; true = handled)

  function handleKey(event) {
    var ctrl = event.modifiers & Qt.ControlModifier
    var key = event.key
    var up = key === Qt.Key_Up || (ctrl && (key === Qt.Key_K || key === Qt.Key_P))
    var down = key === Qt.Key_Down || (ctrl && (key === Qt.Key_J || key === Qt.Key_N))

    if (root.naming) {
      if (key === Qt.Key_Escape) root.naming = false
      else if (key === Qt.Key_Return || key === Qt.Key_Enter) root.saveName()
      else if (Util.editsFilter(event, root.nameDraft)) root.nameDraft = Util.editedFilter(event, root.nameDraft)
      else if (!ctrl && event.text && event.text.length === 1 && event.text.charCodeAt(0) >= 32 && event.text.charCodeAt(0) !== 127)
        root.nameDraft += event.text
      return true
    }

    if (key === Qt.Key_Tab || key === Qt.Key_Backtab) {
      root.tab = root.tab === "folders" ? "people" : "folders"
      root.browsing = false
      root.disarm()
      root.notice = ""
      root.refresh()
      return true
    }

    if (root.tab === "folders" && root.browsing) {
      if (key === Qt.Key_Escape) root.browsing = false
      else if (up) root.browseCursor = Math.max(0, root.browseCursor - 1)
      else if (down) root.browseCursor = Math.min(root.browseRows.length - 1, root.browseCursor + 1)
      else if (key === Qt.Key_Return || key === Qt.Key_Enter || key === Qt.Key_Right) root.activateBrowseRow(root.browseCursor)
      else if ((key === Qt.Key_Left || key === Qt.Key_Backspace) && root.browse.parent) root.request({ op: "browse", path: root.browse.parent })
      else if (key === Qt.Key_Space) {
        var row = root.browseRows[root.browseCursor]
        if (row && row.type === "dir") root.addBrowsed(row.item.path, row.item.name, row.item.added || row.item.covered)
        else root.activateBrowseRow(root.browseCursor)
      }
      return true
    }

    if (key === Qt.Key_Escape) return false   // back to search (the panel handles it)

    if (root.tab === "folders") {
      if (up || down) {
        root.disarm()
        root.folderCursor = Math.max(0, Math.min(root.folderRows.length - 1, root.folderCursor + (up ? -1 : 1)))
      } else if (key === Qt.Key_Return || key === Qt.Key_Enter) root.activateFolderRow(root.folderCursor)
      else if (key === Qt.Key_Space) root.toggleSubfolders(root.folderCursor)
      else if (key === Qt.Key_Delete || key === Qt.Key_Backspace) root.removeFolder(root.folderCursor)
      return true
    }

    // People
    if (!root.people.enabled) {
      if (key === Qt.Key_Return || key === Qt.Key_Enter) root.setFaces(true)
      return true
    }
    var columns = Math.max(1, Math.floor(grid.width / grid.cellWidth))
    var move = key === Qt.Key_Left ? -1 : key === Qt.Key_Right ? 1 : up ? -columns : down ? columns : 0
    if (move !== 0) {
      root.disarm()
      root.personCursor = Math.max(0, Math.min(root.personRows.length - 1, root.personCursor + move))
      grid.positionViewAtIndex(root.personCursor, GridView.Contain)
    } else if (key === Qt.Key_Return || key === Qt.Key_Enter) root.activatePerson(root.personCursor)
    else if (ctrl && key === Qt.Key_Delete) root.setFaces(false)
    else if (key === Qt.Key_Delete) root.forgetPerson(root.personCursor)
    else if (!ctrl && event.text && event.text.length === 1 && event.text.charCodeAt(0) > 32 && event.text.charCodeAt(0) !== 127
             && root.personRows.length > 0)
      root.startNaming(event.text)
    return true
  }

  // ------------------------------------------------------------ view

  Column {
    anchors.fill: parent
    spacing: Style.space(10)

    // Tabs, with what's going on at the right.
    Item {
      id: tabsRow
      width: parent.width
      height: root.panel.headerHeight

      Row {
        id: tabs
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(6)

        Repeater {
          model: [{ id: "folders", label: "󰉋  Folders" }, { id: "people", label: "󰀄  People" }]

          Rectangle {
            required property var modelData
            readonly property bool current: root.tab === modelData.id
            width: tabLabel.implicitWidth + Style.space(24)
            height: tabLabel.implicitHeight + Style.space(12)
            radius: root.panel.cornerRadius
            color: current ? root.panel.selectedBackground : "transparent"

            Text {
              id: tabLabel
              anchors.centerIn: parent
              textFormat: Text.PlainText
              text: modelData.label
              color: parent.current ? root.panel.selectedText : root.panel.foreground
              opacity: parent.current ? 1 : 0.6
              font.family: root.panel.fontFamily
              font.pixelSize: Style.font.title
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                root.tab = modelData.id
                root.browsing = false
                root.naming = false
                root.disarm()
                root.refresh()
              }
            }
          }
        }
      }

      Text {
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.left: tabs.right
        anchors.leftMargin: Style.space(16)
        horizontalAlignment: Text.AlignRight
        textFormat: Text.PlainText
        text: root.tab === "people" && root.people.enabled
          ? (root.people.photos > 0 && root.people.scanned < root.people.photos
              ? "Looking for faces · " + root.count(root.people.scanned) + " of " + root.count(root.people.photos) + " photos"
              : root.count(root.people.faces) + " faces in " + root.count(root.people.photos) + " photos")
          : root.panel.statusLine()
        color: root.panel.foreground
        opacity: 0.5
        elide: Text.ElideLeft
        font.family: root.panel.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Text {
      id: noticeLine
      width: parent.width
      height: Style.font.body + Style.space(8)
      textFormat: Text.PlainText
      text: root.notice || (root.tab === "folders"
        ? (root.browsing ? "Pick a folder to add: Sift reads it, and its subfolders, in the background."
                         : "Sift reads these folders and keeps up with changes. It never changes or moves a file.")
        : (root.people.enabled ? (root.personRows.length === 0
            ? "No faces yet: Sift looks for them after it has read your photos."
            : "Name a face and search for them: “priya at the beach”. Groups are faces that look alike.")
          : ""))
      color: root.panel.foreground
      opacity: 0.7
      elide: Text.ElideRight
      verticalAlignment: Text.AlignVCenter
      font.family: root.panel.fontFamily
      font.pixelSize: Style.font.body
    }

    Item {
      width: parent.width
      height: parent.height - tabsRow.height - noticeLine.height - parent.spacing * 2

      // ---------------------------------------------------- folders
      ListView {
        id: folderList
        anchors.fill: parent
        visible: root.tab === "folders" && !root.browsing
        clip: true
        spacing: Style.space(4)
        boundsBehavior: Flickable.StopAtBounds
        model: root.folderRows
        currentIndex: root.folderCursor
        onCurrentIndexChanged: positionViewAtIndex(currentIndex, ListView.Contain)

        delegate: Rectangle {
          id: folderRow
          required property var modelData
          required property int index
          readonly property bool hasCursor: index === root.folderCursor
          readonly property bool armed: index === root.armedFolder
          width: ListView.view.width
          height: root.panel.rowHeight
          radius: root.panel.cornerRadius
          color: hasCursor ? root.panel.selectedBackground : "transparent"
          opacity: modelData.type === "folder" && !modelData.item.exists ? 0.5 : 1

          Text {
            id: folderGlyph
            anchors.left: parent.left
            anchors.leftMargin: Style.space(14)
            anchors.verticalCenter: parent.verticalCenter
            width: Style.font.heading * 1.6
            textFormat: Text.PlainText
            text: modelData.type === "folder" ? (modelData.item.subfolders ? "󰉓" : "󰉋") : "󰐕"
            color: folderRow.armed ? Color.urgent : folderRow.hasCursor ? root.panel.selectedText : root.panel.foreground
            opacity: 0.85
            font.family: root.panel.fontFamily
            font.pixelSize: Style.font.heading
          }

          Column {
            anchors.left: folderGlyph.right
            anchors.leftMargin: Style.space(8)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(14)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(3)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: folderRow.modelData.type === "folder" ? folderRow.modelData.item.label
                : folderRow.modelData.type === "add" ? "Add a folder…"
                : "Add " + folderRow.modelData.item.label
              color: folderRow.hasCursor ? root.panel.selectedText : root.panel.foreground
              elide: Text.ElideMiddle
              font.family: root.panel.fontFamily
              font.pixelSize: Style.font.title
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: folderRow.modelData.type === "folder" ? root.folderDetail(folderRow.modelData.item)
                : folderRow.modelData.type === "add" ? "Browse your home folder"
                : "Suggested: it's there and not read yet"
              color: folderRow.hasCursor ? root.panel.selectedText : root.panel.foreground
              opacity: 0.6
              elide: Text.ElideRight
              font.family: root.panel.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              root.disarm()
              root.folderCursor = folderRow.index
              if (folderRow.modelData.type !== "folder") root.activateFolderRow(folderRow.index)
            }
          }
        }
      }

      // ---------------------------------------------------- folder browser
      Column {
        anchors.fill: parent
        visible: root.tab === "folders" && root.browsing
        spacing: Style.space(6)

        Text {
          id: browseTitle
          width: parent.width
          textFormat: Text.PlainText
          text: root.browse.label
          color: root.panel.foreground
          opacity: 0.6
          elide: Text.ElideMiddle
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.caption
        }

        ListView {
          width: parent.width
          height: parent.height - browseTitle.height - parent.spacing
          clip: true
          spacing: Style.space(2)
          boundsBehavior: Flickable.StopAtBounds
          model: root.browseRows
          currentIndex: root.browseCursor
          onCurrentIndexChanged: positionViewAtIndex(currentIndex, ListView.Contain)

          delegate: Rectangle {
            id: dirRow
            required property var modelData
            required property int index
            readonly property bool hasCursor: index === root.browseCursor
            readonly property bool here: modelData.type === "here"
            readonly property bool already: !!(here ? (root.browse.added || root.browse.covered)
                                                    : (modelData.item.added || modelData.item.covered))
            width: ListView.view.width
            height: Math.round(root.panel.rowHeight * 0.7)
            radius: root.panel.cornerRadius
            color: hasCursor ? root.panel.selectedBackground : "transparent"

            Text {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(14)
              anchors.right: tag.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: dirRow.here ? "󰐕  Add " + root.browse.label : "󰉋  " + dirRow.modelData.item.name
              color: dirRow.hasCursor ? root.panel.selectedText : root.panel.foreground
              elide: Text.ElideMiddle
              font.family: root.panel.fontFamily
              font.pixelSize: Style.font.body
            }

            Text {
              id: tag
              anchors.right: parent.right
              anchors.rightMargin: Style.space(14)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: dirRow.already ? "already read" : ""
              color: dirRow.hasCursor ? root.panel.selectedText : root.panel.foreground
              opacity: 0.55
              font.family: root.panel.fontFamily
              font.pixelSize: Style.font.caption
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: {
                root.browseCursor = dirRow.index
                root.activateBrowseRow(dirRow.index)
              }
            }
          }
        }
      }

      // ---------------------------------------------------- people: off
      Column {
        anchors.centerIn: parent
        width: parent.width * 0.7
        visible: root.tab === "people" && !root.people.enabled
        spacing: Style.space(12)

        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          text: "󰀄"
          color: root.panel.selectedText
          opacity: 0.8
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.displayLarge
        }

        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.Wrap
          textFormat: Text.PlainText
          text: "Search for people by name: “priya at the beach”, “me and dad”."
          color: root.panel.foreground
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.title
        }

        Text {
          width: parent.width
          horizontalAlignment: Text.AlignHCenter
          wrapMode: Text.Wrap
          textFormat: Text.PlainText
          text: "Sift finds the faces in your photos and groups the ones that look alike; you give a group a name. "
            + "Faceprints stay in the index on this machine, and turning people off deletes them. "
            + "It needs two small models (37 MB)."
          color: root.panel.foreground
          opacity: 0.65
          font.family: root.panel.fontFamily
          font.pixelSize: Style.font.body
        }

        Rectangle {
          anchors.horizontalCenter: parent.horizontalCenter
          width: onLabel.implicitWidth + Style.space(28)
          height: onLabel.implicitHeight + Style.space(14)
          radius: root.panel.cornerRadius
          color: root.panel.selectedBackground

          Text {
            id: onLabel
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: root.working ? "Turning on…" : "↵  Turn on people"
            color: root.panel.selectedText
            font.family: root.panel.fontFamily
            font.pixelSize: Style.font.title
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.setFaces(true)
          }
        }
      }

      // ---------------------------------------------------- people: named and unnamed
      GridView {
        id: grid
        anchors.fill: parent
        visible: root.tab === "people" && root.people.enabled
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        cellWidth: Math.floor(width / Math.max(1, Math.floor(width / Style.space(150))))
        cellHeight: Style.space(110) + Style.font.title + Style.font.caption + Style.space(26)
        model: root.personRows
        currentIndex: root.personCursor

        delegate: Item {
          id: card
          required property var modelData
          required property int index
          readonly property bool hasCursor: index === root.personCursor
          readonly property bool named: modelData.type === "named"
          readonly property bool editing: hasCursor && root.naming
          readonly property bool armed: index === root.armedPerson
          width: grid.cellWidth
          height: grid.cellHeight

          Rectangle {
            anchors.fill: parent
            anchors.margins: Style.space(4)
            radius: root.panel.cornerRadius
            color: card.hasCursor ? root.panel.selectedBackground : "transparent"

            Column {
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.space(6)

              Item {
                id: face
                anchors.horizontalCenter: parent.horizontalCenter
                width: Style.space(110)
                height: width
                layer.enabled: root.panel.cornerRadius > 0
                layer.effect: MultiEffect {
                  maskEnabled: true
                  maskSource: faceMask
                  maskThresholdMin: 0.5
                  maskSpreadAtMin: 1.0
                }

                Image {
                  anchors.fill: parent
                  source: card.named ? (card.modelData.item.thumb ? Util.fileUrl(card.modelData.item.thumb) : "")
                    : (card.modelData.item.thumbs && card.modelData.item.thumbs.length ? Util.fileUrl(card.modelData.item.thumbs[0]) : "")
                  sourceSize.width: 192
                  sourceSize.height: 192
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  smooth: true
                  opacity: card.armed ? 0.35 : 1
                }

                Rectangle {
                  anchors.fill: parent
                  color: "transparent"
                  border.width: card.named ? 0 : Style.normalBorderWidth
                  border.color: Util.alpha(root.panel.foreground, 0.35)
                }
              }

              Rectangle {
                id: faceMask
                width: face.width
                height: face.height
                radius: root.panel.cornerRadius * 2
                visible: false
                layer.enabled: true
              }

              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                textFormat: Text.PlainText
                text: card.editing ? root.nameDraft + "▏"
                  : card.named ? card.modelData.item.name : "Who is this?"
                color: card.hasCursor ? root.panel.selectedText : root.panel.foreground
                opacity: card.named || card.editing ? 1 : 0.6
                elide: Text.ElideRight
                font.family: root.panel.fontFamily
                font.pixelSize: Style.font.title
                font.italic: !card.named && !card.editing
              }

              Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                textFormat: Text.PlainText
                text: card.modelData.item.photos + (card.modelData.item.photos === 1 ? " photo" : " photos")
                color: card.hasCursor ? root.panel.selectedText : root.panel.foreground
                opacity: 0.55
                font.family: root.panel.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              if (root.naming && root.personCursor !== card.index) root.naming = false
              root.disarm()
              root.personCursor = card.index
            }
            onDoubleClicked: root.activatePerson(card.index)
          }
        }
      }
    }
  }
}
