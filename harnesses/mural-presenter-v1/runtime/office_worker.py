"""仅由 Harness 启动的隔离 LibreOffice UNO 客户端，兼容安装自带的 Python。"""
import json
from pathlib import Path
import sys
import time

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException


def properties(**values):
    result = []
    for name, value in values.items():
        item = PropertyValue()
        item.Name, item.Value = name, value
        result.append(item)
    return tuple(result)


def run(request):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    remote = None
    for _ in range(600):
        try:
            remote = resolver.resolve("uno:socket,host=127.0.0.1,port=%s;urp;StarOffice.ComponentContext" % request["port"])
            break
        except NoConnectException:
            time.sleep(0.1)
    if remote is None:
        raise RuntimeError("office_uno_unavailable")
    manager = remote.ServiceManager
    desktop = manager.createInstanceWithContext("com.sun.star.frame.Desktop", remote)
    document = desktop.loadComponentFromURL(Path(request["source"]).as_uri(), "_blank", 0,
        properties(Hidden=True, ReadOnly=True, MacroExecutionMode=0, UpdateDocMode=0,
                   IgnoreReadOnlyRecommended=True))
    if document is None:
        raise RuntimeError("office_document_unavailable")
    try:
        formulas = request["formulas"]
        if request["format"] == "xlsx" and any(not item["has_cached_value"] for item in formulas):
            document.calculateAll()
            for item in formulas:
                if item["has_cached_value"]:
                    continue
                cell = document.Sheets.getByName(item["worksheet"]).getCellRangeByName(item["cell"])
                error = cell.getError()
                if error:
                    item["calculation_error"] = int(error)
                else:
                    result_type = int(cell.getPropertyValue("FormulaResultType2"))
                    value = cell.getString() if result_type == 2 else cell.getValue()
                    item.update(calculated_value=value, calculated_text=cell.getString(),
                                calculation="libreoffice_calc_explicit", original_cache_missing=True)
        groups = []
        if request["format"] == "xlsx":
            for name in document.Sheets.getElementNames():
                groups.append(({"worksheet": name}, document.Sheets.getByName(name).DrawPage))
        elif request["format"] == "pptx":
            groups = [({"slide": index + 1}, document.DrawPages.getByIndex(index))
                      for index in range(document.DrawPages.Count)]
        else:
            groups = [({}, document.DrawPage)]
        charts = []

        def visit(shape, location):
            if shape.supportsService("com.sun.star.drawing.GroupShape"):
                for index in range(shape.Count):
                    visit(shape.getByIndex(index), location)
                return
            model = getattr(shape, "Model", None)
            if (model is None or not hasattr(model, "supportsService")
                    or not model.supportsService("com.sun.star.chart2.ChartDocument")):
                return
            if request["format"] == "docx":
                location = {**location, "document_object": shape.getName()}
            filename = "chart_%03d.png" % (len(charts) + 1)
            exporter = manager.createInstanceWithContext("com.sun.star.drawing.GraphicExportFilter", remote)
            # Writer 的嵌入框不是导出器接受的 Draw shape，使用同一原生 chart 的绘图页。
            if request["format"] == "docx":
                model.createInstance("com.sun.star.chart2.ChartView").update()
            exporter.setSourceDocument(model.getDrawPage() if request["format"] == "docx" else shape)
            width, height = shape.Size.Width, shape.Size.Height
            pixel_width = min(2400, max(1, int(width * 144 / 2540)))
            pixel_height = min(2400, max(1, int(height * 144 / 2540)))
            if not exporter.filter(properties(URL=(Path(request["directory"]) / filename).as_uri(),
                    MediaType="image/png", FilterData=properties(PixelWidth=pixel_width, PixelHeight=pixel_height))):
                raise RuntimeError("office_chart_export_failed")
            charts.append({**location, "chart_index": len(charts) + 1, "filename": filename,
                           "derivation": "libreoffice_native_chart", "visual_verified": False})

        for location, page in groups:
            for index in range(page.Count):
                visit(page.getByIndex(index), location)
        if request["preview"]:
            kind = {"docx": "writer", "pptx": "impress", "xlsx": "calc"}[request["format"]]
            document.storeToURL((Path(request["directory"]) / "preview.pdf").as_uri(),
                                properties(FilterName=kind + "_pdf_Export", Overwrite=True))
        config = manager.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", remote)
        product = config.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess",
                                                      properties(nodepath="/org.openoffice.Setup/Product"))
        return {"charts": charts, "formulas": formulas, "engine_version": product.ooSetupVersionAboutBox}
    finally:
        document.close(True)
        desktop.terminate()


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
