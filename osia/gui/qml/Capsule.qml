pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Shapes

Item {
    id: root

    property string activity: "idle"
    property string capsuleShape: "capsule"
    property string dockSide: ""
    property bool dockExposed: false
    property bool reducedMotion: false
    required property real launchWidth
    required property real launchHeight
    required property real dockedRadius
    required property real circleActiveScale
    required property real thinkingPeriod
    property real launchProgress: 0
    property real elapsedMs: 0
    property real speechPhase: 0
    property int themeDuration: 0
    property color backgroundColor
    property color primaryColor
    property color secondaryColor
    property color textColor
    property color railColor

    readonly property bool docked: capsuleShape === "docked"
    readonly property bool circular: capsuleShape === "circle"
    readonly property bool activeSurface: !docked || dockExposed
    readonly property real cycle: (elapsedMs % thinkingPeriod) / thinkingPeriod
    readonly property real tau: 2 * Math.PI

    function alpha(color, value) {
        return Qt.rgba(color.r, color.g, color.b, value / 255)
    }

    component PaletteFade: Behavior {
        enabled: root.themeDuration > 0
        ColorAnimation { duration: root.themeDuration; easing.type: Easing.OutCubic }
    }

    PaletteFade on backgroundColor {}
    PaletteFade on primaryColor {}
    PaletteFade on secondaryColor {}
    PaletteFade on textColor {}
    PaletteFade on railColor {}

    Rectangle {
        id: background
        anchors.centerIn: parent
        width: Math.max(0, Math.min(root.width,
            root.launchWidth + (root.width - root.launchWidth) * root.launchProgress) - 1)
        height: Math.max(0, Math.min(root.height,
            root.launchHeight + (root.height - root.launchHeight) * root.launchProgress) - 1)
        radius: Math.min(width / 2, height / 2, root.docked ? root.dockedRadius : Number.POSITIVE_INFINITY)
        color: root.backgroundColor
        border.color: root.alpha(root.primaryColor, 31)
        border.width: 1
        antialiasing: true
    }

    Rectangle {
        visible: root.docked && !root.dockExposed
        x: background.x + background.width * (root.dockSide === "right" ? 0.25 : 0.75) - width / 2
        anchors.verticalCenter: parent.verticalCenter
        width: 1
        height: background.height - Math.min(20, background.height / 2)
        radius: width / 2
        color: root.railColor
    }

    Text {
        anchors.centerIn: parent
        visible: !root.docked && (root.activity === "idle" || (root.circular && root.activity === "thinking"))
        text: root.circular ? "O" : "OSIA"
        color: root.alpha(root.textColor, 200)
        font.pixelSize: root.circular ? (root.activity === "idle" ? 30
            : Math.max(1, Math.min(18, Math.round(background.width * 0.33)))) : 12
        font.bold: true
        font.letterSpacing: root.circular ? 0 : 3
        opacity: root.activity === "idle" ? Math.min(1, Math.max(0, (root.launchProgress - 0.7) / 0.3)) : 1
    }

    Item {
        id: listening
        anchors.fill: parent
        visible: root.activeSurface && root.activity === "listening"
        scale: root.circular ? root.circleActiveScale : 1

        Repeater {
            model: 3
            delegate: Rectangle {
                required property int index
                readonly property real t: (root.elapsedMs / 1800 + index / 3) % 1
                readonly property real pulse: Math.pow(Math.sin(Math.PI * t), 2)
                anchors.centerIn: parent
                width: root.docked ? 12 + 12 * t : 26 + 22 * t
                height: root.docked ? 30 + 42 * t : width
                radius: width / 2
                color: "transparent"
                border.color: root.alpha(root.primaryColor, (root.docked ? 130 : 145) * pulse)
                border.width: root.docked ? 1.3 : 1.6
                antialiasing: true
            }
        }

        Shape {
            id: microphone
            anchors.centerIn: parent
            width: 20
            height: 24
            scale: root.docked ? 0.72 : root.reducedMotion ? 1
                : 1 + 0.10 * Math.sin(Math.PI * Math.min(1, root.elapsedMs / 420))
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeColor: root.primaryColor
                strokeWidth: 1.5
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                startX: 10.6
                startY: 2
                PathCubic { control1X: 15.2; control1Y: 2.8; control2X: 15.6; control2Y: 8.5; x: 14.4; y: 11.8 }
                PathCubic { control1X: 13.4; control1Y: 14.6; control2X: 6.4; control2Y: 14.8; x: 5.4; y: 11.6 }
                PathCubic { control1X: 4.4; control1Y: 8.2; control2X: 5; control2Y: 3; x: 9.4; y: 2 }
                PathMove { x: 2.4; y: 10.5 }
                PathCubic { control1X: 2.8; control1Y: 17.5; control2X: 17.2; control2Y: 17.5; x: 17.6; y: 10.5 }
                PathMove { x: 10.2; y: 17.3 }
                PathCubic { control1X: 10.5; control1Y: 19.4; control2X: 9.5; control2Y: 20.6; x: 10.2; y: 22.4 }
                PathMove { x: 6.6; y: 21.8 }
                PathCubic { control1X: 8.6; control1Y: 23.4; control2X: 11.4; control2Y: 23.4; x: 13.4; y: 21.8 }
            }
        }
    }

    Item {
        id: thinking
        anchors.fill: parent
        visible: root.activeSurface && root.activity === "thinking"
        rotation: root.docked ? 90 : 0

        Repeater {
            model: 6
            delegate: Item {
                id: light
                required property int index
                readonly property real phase: root.tau * (root.cycle - index / 6)
                readonly property real wave: Math.sin(phase)
                readonly property real pulse: (1 + (root.circular ? Math.cos(phase) : wave)) / 2
                readonly property real orbit: Math.min(background.width, background.height) * 0.33
                readonly property real angle: root.tau * (root.cycle + index / 6) - Math.PI / 2
                readonly property real extent: root.docked ? Math.min(54, background.height * 0.66)
                    : Math.min(80, background.width * 0.68)
                readonly property real lightRadius: root.circular ? 1.8 + 1.1 * pulse
                    : (root.docked ? 1.9 : 2.2) + 1.25 * pulse

                x: root.width / 2 + (root.circular ? orbit * Math.cos(angle) : (index / 5 - 0.5) * extent)
                y: root.height / 2 + (root.circular ? orbit * Math.sin(angle) : -2.8 * wave)

                Rectangle {
                    anchors.centerIn: parent
                    width: (light.lightRadius + (root.circular ? 2 : 2.5)) * 2
                    height: width
                    radius: width / 2
                    color: root.alpha(root.primaryColor, root.circular ? 14 + 18 * light.pulse : 12 + 20 * light.pulse)
                    antialiasing: true
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: light.lightRadius * 2
                    height: width
                    radius: width / 2
                    color: root.alpha(root.primaryColor, root.circular ? 95 + 150 * light.pulse : 85 + 160 * light.pulse)
                    antialiasing: true
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: light.lightRadius * 0.8
                    height: width
                    radius: width / 2
                    color: root.alpha(root.textColor, 150 * light.pulse)
                    antialiasing: true
                }
            }
        }
    }

    Item {
        anchors.centerIn: parent
        visible: !root.docked && root.activity === "speaking"
        width: 67
        height: parent.height
        scale: root.circular ? root.circleActiveScale : 1

        Repeater {
            model: 9
            delegate: Rectangle {
                required property int index
                readonly property real amplitude: Math.max(2, 14 - Math.abs(index - 4) * 2.4)
                anchors.verticalCenter: parent.verticalCenter
                x: index * 8
                width: 3
                height: 2 * amplitude * (0.4 + 0.6 * Math.abs(Math.sin(root.speechPhase * 2.6 + index * 0.7)))
                radius: width / 2
                color: root.alpha(root.primaryColor, 220)
                antialiasing: true
            }
        }
    }

    Rectangle {
        id: speechRail
        anchors.centerIn: parent
        visible: root.docked && root.dockExposed && root.activity === "speaking"
        width: 4
        height: Math.min(36, background.height * 0.66)
        radius: width / 2
        color: root.alpha(root.primaryColor, 45)
        clip: true

        Rectangle {
            width: parent.width
            height: parent.height * 0.54
            y: parent.height * (0.23 + 0.28 * Math.sin(root.tau * root.cycle))
            radius: width / 2
            gradient: Gradient {
                GradientStop { position: 0; color: root.alpha(root.primaryColor, 0) }
                GradientStop { position: 0.30; color: root.alpha(root.secondaryColor, 190) }
                GradientStop { position: 0.62; color: root.alpha(root.textColor, 240) }
                GradientStop { position: 1; color: root.alpha(root.primaryColor, 0) }
            }
        }
    }
}
