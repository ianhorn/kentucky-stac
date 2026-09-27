"""Persist drawn AOIs as features in scratch (memory) layers, so the user keeps an editable,
re-selectable record of what they drew -- the QGIS counterpart of the old Map Notes layers."""

from __future__ import annotations

from typing import Optional

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
)

KIND_PROPERTY = "kentucky_stac/aoi_kind"

# geometry type -> (layer name suffix, memory provider geometry string)
_KINDS = {
    Qgis.GeometryType.Point: ("Point", "Point"),
    Qgis.GeometryType.Line: ("Line", "LineString"),
    Qgis.GeometryType.Polygon: ("Polygon", "Polygon"),
}


def _find_layer(project: QgsProject, kind: str) -> Optional[QgsVectorLayer]:
    for layer in project.mapLayers().values():
        if layer.customProperty(KIND_PROPERTY) == kind:
            return layer
    return None


def _create_layer(project: QgsProject, geometry_type, crs: QgsCoordinateReferenceSystem) -> QgsVectorLayer:
    kind, provider_geometry = _KINDS[geometry_type]
    layer = QgsVectorLayer(f"{provider_geometry}?field=label:string", f"Ky STAC AOI ({kind})", "memory")
    layer.setCrs(crs)
    layer.setCustomProperty(KIND_PROPERTY, kind)
    if geometry_type == Qgis.GeometryType.Polygon:
        layer.renderer().setSymbol(
            QgsFillSymbol.createSimple(
                {"color": "0,200,255,60", "outline_color": "0,160,220,255", "outline_width": "0.8"}
            )
        )
    project.addMapLayer(layer)
    return layer


def add_aoi_feature(
    geometry: QgsGeometry,
    crs: QgsCoordinateReferenceSystem,
    label: str,
    project: Optional[QgsProject] = None,
) -> QgsVectorLayer:
    """Append a drawn AOI to the scratch layer for its geometry type, creating the layer if needed."""
    project = project or QgsProject.instance()
    geometry_type = geometry.type()
    if geometry_type not in _KINDS:
        raise ValueError("Unsupported AOI geometry type")
    kind = _KINDS[geometry_type][0]

    layer = _find_layer(project, kind) or _create_layer(project, geometry_type, crs)
    g = QgsGeometry(geometry)
    if layer.crs() != crs:
        g.transform(QgsCoordinateTransform(crs, layer.crs(), project.transformContext()))

    feature = QgsFeature(layer.fields())
    feature.setGeometry(g)
    feature.setAttribute("label", label)
    if not layer.dataProvider().addFeatures([feature])[0]:
        raise RuntimeError(f"Could not add the AOI to the {layer.name()} layer")
    layer.updateExtents()
    layer.triggerRepaint()
    return layer
