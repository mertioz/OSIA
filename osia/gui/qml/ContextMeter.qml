import QtQuick
import QtQuick.Shapes

Item {
    id: meter

    required property color trackColor
    required property color usageColor
    property real fraction: 0

    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer

        ShapePath {
            fillColor: "transparent"
            strokeColor: meter.trackColor
            strokeWidth: 1.5

            PathAngleArc {
                centerX: meter.width / 2
                centerY: meter.height / 2
                radiusX: 6.5
                radiusY: radiusX
                startAngle: -90
                sweepAngle: 360
            }
        }

        ShapePath {
            fillColor: "transparent"
            strokeColor: meter.fraction > 0 ? meter.usageColor : "transparent"
            strokeWidth: 1.5
            capStyle: ShapePath.RoundCap

            PathAngleArc {
                centerX: meter.width / 2
                centerY: meter.height / 2
                radiusX: 6.5
                radiusY: radiusX
                startAngle: -90
                sweepAngle: meter.fraction * 360
            }
        }
    }
}
