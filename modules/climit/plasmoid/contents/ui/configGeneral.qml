import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: root

    // Aliases wire the fields to the KConfigXT keys declared in config/main.xml.
    property alias cfg_refreshSec: refreshSpin.value
    property alias cfg_showFiveHour: showFiveHour.checked
    property alias cfg_showSevenDay: showSevenDay.checked
    property alias cfg_showAllWindowsInPopup: showAllWindows.checked

    QQC2.SpinBox {
        id: refreshSpin
        Kirigami.FormData.label: i18n("Refresh every (seconds):")
        from: 5
        to: 600
        stepSize: 5
    }

    QQC2.CheckBox {
        id: showFiveHour
        Kirigami.FormData.label: i18n("Panel shows:")
        text: i18n("5-hour")
    }
    QQC2.CheckBox {
        id: showSevenDay
        text: i18n("weekly")
    }

    QQC2.CheckBox {
        id: showAllWindows
        Kirigami.FormData.label: i18n("Popup shows:")
        text: i18n("per-model weekly windows (Opus / Sonnet)")
    }
}
