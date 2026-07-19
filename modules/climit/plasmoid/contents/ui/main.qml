import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    // ---- state, filled from `climit status --json --no-poll` ----
    property var allWindows: []       // every window the CLI would print
    property var windows: []          // allowedWindows subset (compact + tooltip)
    property var cross: null          // cli.py:cross_metric, or null when idle
    property bool anyWarn: false      // any window projected to cap before reset
    property bool haveData: false
    property string lastError: ""
    property double lastUpdateMs: 0   // parsed.now_ms of the last good read

    // The climit token below is replaced with the absolute nix store path at
    // build time (icedos.nix), so PATH in the plasmashell session is irrelevant.
    readonly property string climitCmd: "@climit@ status --json --no-poll"

    // Only these windows reach the compact face and the tooltip; the per-model
    // Opus/Sonnet weekly windows would swamp a panel item. The popup shows them
    // too unless showAllWindowsInPopup is unticked.
    readonly property var allowedWindows: ["five_hour", "seven_day"]

    // ---- formatting: kept identical to cli.py so panel and CLI read the same ----
    function fmtDur(ms) {
        if (ms === null || ms === undefined) return "—"; // em dash
        var s = Math.max(0, Math.floor(ms / 1000));
        var d = Math.floor(s / 86400); s -= d * 86400;
        var h = Math.floor(s / 3600);  s -= h * 3600;
        var m = Math.floor(s / 60);    s -= m * 60;
        if (d) return d + "d" + h + "h";
        if (h) return h + "h" + m + "m";
        if (m) return m + "m";
        return s + "s";
    }

    function utilColor(u) {
        if (u < 50) return Kirigami.Theme.positiveTextColor;   // green
        if (u < 80) return Kirigami.Theme.neutralTextColor;    // amber
        return Kirigami.Theme.negativeTextColor;               // red
    }

    // mirrors config.WINDOW_LABELS
    function labelFor(w) {
        return ({
            "five_hour": "5-hour",
            "seven_day": "weekly",
            "seven_day_opus": "week · Opus",
            "seven_day_sonnet": "week · Sonnet"
        })[w] || w;
    }

    function shortLabel(w) {
        return ({
            "five_hour": "5h",
            "seven_day": "wk"
        })[w] || w.substring(0, 3);
    }

    // Whether a window is ticked for the compact (panel) face.
    function windowShown(w) {
        var c = Plasmoid.configuration;
        switch (w) {
            case "five_hour": return c.showFiveHour;
            case "seven_day": return c.showSevenDay;
        }
        return true;
    }

    // Present windows ticked for the compact face.
    function compactWindows() {
        return windows.filter(function (r) { return windowShown(r.window); });
    }

    // Windows listed in the popup: CLI parity, or just the two main ones.
    function popupWindows() {
        return Plasmoid.configuration.showAllWindowsInPopup ? allWindows : windows;
    }

    // reset countdown for a row, NaN when the window has no reset timestamp
    function resetIn(r) {
        return r.reset_ts ? (r.reset_ts - root.nowMs) : NaN;
    }

    function runwayText(r) {
        return r.runway_min === null ? "∞" : fmtDur(r.runway_min * 60000);
    }

    // ---- per-window metric strip, same numbers cli.py:render_table prints ----
    readonly property var metricLabels: ["%/min", "%/hr", "%/8h", "%/day", "ends in", "resets in"]

    function metricValue(r, i) {
        switch (i) {
            case 0: return r.per_min.toFixed(2);
            case 1: return r.per_hour.toFixed(1);
            case 2: return r.per_8h.toFixed(1);
            case 3: return r.per_day.toFixed(1);
            case 4: return (r.will_exhaust_before_reset ? "⚠ " : "") + runwayText(r);
            case 5: var m = resetIn(r); return isNaN(m) ? "—" : fmtDur(m);
        }
        return "";
    }

    function metricColor(r, i) {
        return (i === 4 && r.will_exhaust_before_reset)
               ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor;
    }

    // cli.py:render_cross, shortened to fit a popup footer
    function crossText() {
        if (!root.cross) return "";
        var t = "1% wk ≈ " + root.cross.session_pct_per_weekly_pct.toFixed(1) + "% of 5h";
        if (root.cross.weekly_pct_until_session_caps !== null
            && root.cross.weekly_pct_until_session_caps !== undefined)
            t += "  ·  ~" + Math.round(root.cross.weekly_pct_until_session_caps)
               + "% more wk before 5h caps";
        return t;
    }

    // ---- data acquisition ----
    P5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: function (sourceName, data) {
            disconnectSource(sourceName); // one-shot per run
            var exitCode = data["exit code"];
            var stdout = data["stdout"] || "";
            if (exitCode !== 0) {
                root.lastError = (data["stderr"] || "").trim() || ("exit " + exitCode);
                return;
            }
            try {
                var parsed = JSON.parse(stdout);
                var all = parsed.windows || [];
                var ws = all.filter(function (r) {
                    return root.allowedWindows.indexOf(r.window) !== -1;
                });
                var warn = false;
                for (var i = 0; i < ws.length; i++)
                    warn = warn || ws[i].will_exhaust_before_reset;
                root.allWindows = all;
                root.windows = ws;
                root.cross = parsed.cross || null;
                root.anyWarn = warn;
                root.haveData = true;
                root.lastUpdateMs = parsed.now_ms || Date.now();
                root.lastError = "";
            } catch (e) {
                root.lastError = "parse error: " + e;
            }
        }
    }

    function refresh() {
        // disconnect any stale run before starting a fresh one
        executable.connectSource(root.climitCmd);
    }

    Timer {
        interval: Math.max(5, Plasmoid.configuration.refreshSec) * 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    // clock so reset/runway countdowns tick between data refreshes
    property double nowMs: 0
    Timer {
        interval: 1000
        running: root.expanded || Plasmoid.location === PlasmaCore.Types.Floating
        repeat: true
        triggeredOnStart: true
        onTriggered: root.nowMs = Date.now()
    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Refresh Now")
            icon.name: "view-refresh"
            onTriggered: root.refresh()
        }
    ]

    toolTipMainText: "climit"
    toolTipSubText: {
        if (!root.windows.length)
            return root.lastError ? root.lastError : "no data yet";
        var lines = root.windows.map(function (r) {
            var reset = root.resetIn(r);
            return labelFor(r.window) + "  " + Math.round(r.util) + "% · "
                 + r.per_hour.toFixed(1) + "%/h"
                 + (r.runway_min === null ? "" : " · ends " + root.runwayText(r))
                 + (isNaN(reset) ? "" : " · resets " + root.fmtDur(reset));
        });
        if (root.anyWarn)
            lines.unshift("⚠ projected to cap before reset");
        return lines.join("\n");
    }

    // ---- compact (panel / system tray) face ----
    compactRepresentation: MouseArea {
        id: compact
        hoverEnabled: true

        // Plasma closes the popup on the press that lands outside it, so by the
        // time onClicked runs `root.expanded` is already false and a plain toggle
        // would immediately re-open it. Latch the pre-press state instead.
        property bool wasExpanded: false
        onPressed: compact.wasExpanded = root.expanded
        onClicked: root.expanded = !compact.wasExpanded

        property var rows: root.compactWindows()

        Layout.minimumWidth: row.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: Layout.minimumWidth

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: Kirigami.Units.largeSpacing

            // fallback icon when nothing is shown (no data, or all unticked)
            Kirigami.Icon {
                source: "speedometer"
                visible: compact.rows.length === 0
                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                Layout.preferredHeight: Kirigami.Units.iconSizes.small
            }

            // one "5h 30%" chunk per shown window, all at once
            Repeater {
                model: compact.rows
                delegate: RowLayout {
                    spacing: Kirigami.Units.smallSpacing

                    PlasmaComponents.Label {
                        text: root.shortLabel(modelData.window)
                        opacity: 0.8
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                    }
                    PlasmaComponents.Label {
                        text: Math.round(modelData.util) + "%"
                        color: root.utilColor(modelData.util)
                        font.bold: true
                    }
                    PlasmaComponents.Label {
                        visible: modelData.will_exhaust_before_reset
                        text: "⚠" // warning sign
                        color: Kirigami.Theme.negativeTextColor
                        font.bold: true
                    }
                }
            }
        }
    }

    // ---- full (popup / desktop) face ----
    // One section per window — heading + used%, a full-width usage bar, then the
    // metric strip (%/min · %/hr · %/8h · %/day · ends in · resets in). Same
    // numbers as cli.py:render_table, stacked instead of squeezed into one row,
    // with render_cross's exchange line in the footer.
    fullRepresentation: PlasmaExtras.Representation {
        id: rep

        readonly property var rows: root.popupWindows()
        readonly property real cellFont: Kirigami.Theme.smallFont.pointSize

        Layout.minimumWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredWidth: Kirigami.Units.gridUnit * 26
        // Height hints cover the WHOLE representation, footer included — leave the
        // footer out and it overlaps the last section instead of sitting under it.
        readonly property real chromeHeight: footerBar.implicitHeight + topPadding + bottomPadding
                                             + Kirigami.Units.smallSpacing * 2
        // floor keeps the "no data yet" placeholder readable when there are no rows
        Layout.minimumHeight: Math.max(Kirigami.Units.gridUnit * 8,
                                       sections.implicitHeight + chromeHeight)
        Layout.preferredHeight: Layout.minimumHeight

        collapseMarginsHint: true

        Item {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.smallSpacing

            // empty / error state (mirrors render_table's message)
            PlasmaExtras.PlaceholderMessage {
                anchors.centerIn: parent
                width: parent.width - Kirigami.Units.gridUnit * 4
                visible: rep.rows.length === 0
                iconName: "speedometer"
                text: root.lastError ? i18n("climit unavailable") : i18n("No data yet")
                explanation: root.lastError
                             ? root.lastError
                             : "Start the climit daemon:\nsystemctl --user start climit"
            }

            ColumnLayout {
                id: sections

                anchors.fill: parent
                visible: rep.rows.length > 0
                spacing: Kirigami.Units.largeSpacing

                Repeater {
                    model: rep.rows

                    delegate: ColumnLayout {
                        id: section

                        readonly property var win: modelData

                        Layout.fillWidth: true
                        // leftover height is shared between the sections rather
                        // than pooling at the bottom of the popup
                        Layout.fillHeight: true
                        spacing: Kirigami.Units.smallSpacing
                        opacity: section.win.stale ? 0.5 : 1.0

                        Kirigami.Separator {
                            Layout.fillWidth: true
                            visible: index > 0
                            opacity: 0.3
                        }

                        // heading row: window name (+ ·stale / cap warning) and used%
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Kirigami.Heading {
                                level: 4
                                text: root.labelFor(section.win.window)
                            }
                            PlasmaComponents.Label {
                                visible: section.win.stale
                                text: "·stale"
                                opacity: 0.7
                                font.pointSize: rep.cellFont
                            }
                            Item { Layout.fillWidth: true }
                            PlasmaComponents.Label {
                                visible: section.win.will_exhaust_before_reset
                                text: "⚠ hits cap before reset"
                                color: Kirigami.Theme.negativeTextColor
                                font.pointSize: rep.cellFont
                            }
                            Kirigami.Heading {
                                level: 4
                                text: Math.round(section.win.util) + "%"
                                color: root.utilColor(section.win.util)
                            }
                        }

                        // usage bar, standing in for the CLI's █░ column
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.round(Kirigami.Units.gridUnit * 0.6)
                            radius: Kirigami.Units.cornerRadius
                            color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                                           Kirigami.Theme.textColor.b, 0.12)

                            Rectangle {
                                anchors.left: parent.left
                                anchors.verticalCenter: parent.verticalCenter
                                height: parent.height
                                radius: parent.radius
                                // never collapse to nothing: 1% still shows a sliver
                                width: Math.max(parent.height,
                                                parent.width * Math.max(0, Math.min(1, section.win.util / 100)))
                                color: root.utilColor(section.win.util)
                            }
                        }

                        // metric strip: labels on top, values under them, evenly
                        // spread so the row spans the whole popup width
                        GridLayout {
                            Layout.fillWidth: true
                            columns: root.metricLabels.length
                            columnSpacing: Kirigami.Units.smallSpacing
                            rowSpacing: 0

                            Repeater {
                                model: root.metricLabels.length * 2

                                delegate: PlasmaComponents.Label {
                                    readonly property int col: index % root.metricLabels.length
                                    readonly property bool isLabel: index < root.metricLabels.length

                                    Layout.fillWidth: true
                                    horizontalAlignment: Text.AlignHCenter
                                    elide: Text.ElideRight
                                    font.pointSize: rep.cellFont
                                    opacity: isLabel ? 0.6 : 1.0
                                    text: isLabel ? root.metricLabels[col]
                                                  : root.metricValue(section.win, col)
                                    color: isLabel ? Kirigami.Theme.textColor
                                                   : root.metricColor(section.win, col)
                                }
                            }
                        }
                    }
                }
            }
        }

        footer: PlasmaExtras.PlasmoidHeading {
            id: footerBar

            position: PlasmaExtras.PlasmoidHeading.Footer

            RowLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.smallSpacing

                // render_cross's exchange line
                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    visible: root.cross !== null
                    elide: Text.ElideRight
                    opacity: 0.7
                    font.pointSize: rep.cellFont
                    text: root.crossText()

                    PlasmaComponents.ToolTip.text: text
                    PlasmaComponents.ToolTip.visible: crossHover.hovered && truncated
                    PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
                    HoverHandler { id: crossHover }
                }
                Item { Layout.fillWidth: true; visible: root.cross === null }

                PlasmaComponents.Label {
                    visible: root.lastUpdateMs > 0
                    opacity: 0.5
                    font.pointSize: rep.cellFont
                    text: "updated " + root.fmtDur(Math.max(0, root.nowMs - root.lastUpdateMs)) + " ago"
                }
                PlasmaComponents.ToolButton {
                    icon.name: "view-refresh"
                    display: QQC2.AbstractButton.IconOnly
                    text: i18n("Refresh Now")
                    onClicked: root.refresh()

                    PlasmaComponents.ToolTip.text: text
                    PlasmaComponents.ToolTip.visible: hovered
                    PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
            }
        }
    }
}
