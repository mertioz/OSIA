pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

Item {
    id: edge

    required property color accent
    property real phase: 0
    property real reveal: 0

    readonly property real highlightPosition: 0.5 + 0.34 * Math.sin(phase * 0.75)
    readonly property color highlightColor: Qt.lighter(accent, 1.35)
    readonly property color clearAccent: tinted(accent, 0)
    readonly property var wave: {
        const points = []
        for (let i = 0; i <= 64; ++i) {
            const portion = i / 64
            const offset = 0.8 * Math.sin(portion * 2 * Math.PI + phase)
            points.push(Qt.point(width * portion, height - 3.5 + offset))
        }
        return points
    }

    function tinted(color, alpha) {
        return Qt.rgba(color.r, color.g, color.b, alpha / 255)
    }

    function ribbon(halfHeight) {
        // Use a filled outline so the gradient stays in the scene graph.
        const points = wave.map(point => Qt.point(point.x, point.y - halfHeight))
        for (let i = wave.length - 1; i >= 0; --i)
            points.push(Qt.point(wave[i].x, wave[i].y + halfHeight))
        points.push(points[0])
        return points
    }

    Repeater {
        model: [
            { thickness: 22, alpha: 8 },
            { thickness: 14, alpha: 16 },
            { thickness: 8, alpha: 32 },
            { thickness: 3.5, alpha: 120 },
            { thickness: 1.2, alpha: 245 }
        ]

        Shape {
            id: glow

            required property var modelData
            readonly property color baseColor: edge.tinted(edge.accent, Math.round(modelData.alpha * 0.65))
            readonly property color crestColor: edge.tinted(edge.highlightColor, modelData.alpha)

            anchors.fill: edge
            opacity: edge.reveal
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeWidth: -1
                fillGradient: LinearGradient {
                    x2: edge.width
                    GradientStop { position: 0; color: edge.clearAccent }
                    GradientStop { position: 0.12; color: glow.baseColor }
                    GradientStop {
                        position: 0.5
                        color: edge.highlightPosition === 0.5 ? glow.crestColor : glow.baseColor
                    }
                    GradientStop { position: 0.88; color: glow.baseColor }
                    GradientStop { position: 1; color: edge.clearAccent }
                    GradientStop { position: Math.max(0.01, edge.highlightPosition - 0.12); color: glow.baseColor }
                    GradientStop { position: edge.highlightPosition; color: glow.crestColor }
                    GradientStop { position: Math.min(0.99, edge.highlightPosition + 0.12); color: glow.baseColor }
                }

                PathPolyline { path: edge.ribbon(glow.modelData.thickness / 2) }
            }
        }
    }

    Repeater {
        model: [{ offset: 12, alpha: 75 }, { offset: 19, alpha: 30 }]

        Shape {
            id: reflection

            required property var modelData

            anchors.fill: edge
            opacity: edge.reveal
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeWidth: -1
                fillGradient: LinearGradient {
                    x2: edge.width
                    GradientStop { position: 0; color: edge.clearAccent }
                    GradientStop {
                        position: 0.5 + 0.15 * Math.sin(edge.phase * 0.5)
                        color: edge.tinted(edge.accent, reflection.modelData.alpha)
                    }
                    GradientStop { position: 1; color: edge.clearAccent }
                }

                PathRectangle {
                    x: edge.width * 0.03
                    y: edge.height - reflection.modelData.offset - 0.4
                    width: edge.width * 0.94
                    height: 0.8
                }
            }
        }
    }
}
