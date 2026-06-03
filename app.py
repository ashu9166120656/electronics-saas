"""PCB Trace Width Calculator — IPC-2221 formula."""

from math import log, sqrt

from flask import Flask, render_template, request

app = Flask(__name__)

# IPC-2221 constants
K_OUTER = 0.048
K_INNER = 0.024

# 1 oz/ft² copper thickness in mm
COPPER_OZ_TO_MM = 0.0347
MM_TO_MILS = 39.37


def calc_trace_width(current_a, temp_rise_c, copper_oz, layer):
    """
    Compute trace width from IPC-2221.

        I = k * ΔT^0.44 * A^0.725
    =>  A = (I / (k * ΔT^0.44)) ^ (1 / 0.725)

    width_mils = A / thickness_mils
    width_mm   = width_mils / 39.37
    """
    k = K_OUTER if layer == "outer" else K_INNER

    if current_a <= 0 or temp_rise_c <= 0 or copper_oz <= 0:
        return None

    area_sq_mils = (current_a / (k * (temp_rise_c ** 0.44))) ** (1 / 0.725)

    thickness_mils = copper_oz * COPPER_OZ_TO_MM * MM_TO_MILS  # ~1.366 per oz
    width_mils = area_sq_mils / thickness_mils
    width_mm = width_mils / MM_TO_MILS

    return {
        "width_mm": round(width_mm, 4),
        "width_mils": round(width_mils, 2),
        "area_sq_mils": round(area_sq_mils, 2),
        "k": k,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None
    form_data = {}

    if request.method == "POST":
        try:
            current = float(request.form.get("current", 0))
            temp_rise = float(request.form.get("temp_rise", 0))
            copper = float(request.form.get("copper", 1))
            layer = request.form.get("layer", "outer")

            form_data = {
                "current": request.form.get("current", "1"),
                "temp_rise": request.form.get("temp_rise", "10"),
                "copper": request.form.get("copper", "1"),
                "layer": layer,
            }

            result = calc_trace_width(current, temp_rise, copper, layer)
            if result is None:
                error = "All input values must be positive numbers greater than zero."

        except (ValueError, TypeError):
            error = "Please enter valid numeric values for all fields."

    return render_template("index.html", result=result, error=error, form_data=form_data)


@app.route("/impedance", methods=["GET", "POST"])
def impedance():
    result = None
    error = None
    form_data = {}

    if request.method == "POST":
        try:
            trace_w = float(request.form.get("trace_w", 0))
            dielectric_h = float(request.form.get("dielectric_h", 0))
            er = float(request.form.get("er", 0))
            copper_oz = float(request.form.get("copper_oz", 1))

            form_data = {
                "trace_w": request.form.get("trace_w", "0.5"),
                "dielectric_h": request.form.get("dielectric_h", "0.2"),
                "er": request.form.get("er", "4.5"),
                "copper_oz": request.form.get("copper_oz", "1"),
            }

            if trace_w <= 0 or dielectric_h <= 0 or er <= 0 or copper_oz <= 0:
                error = "All input values must be positive numbers greater than zero."
            else:
                t_mm = copper_oz * COPPER_OZ_TO_MM
                z0 = (87 / sqrt(er + 1.41)) * log(5.98 * dielectric_h / (0.8 * trace_w + t_mm))
                result = {
                    "z0": round(z0, 1),
                    "trace_w": trace_w,
                    "dielectric_h": dielectric_h,
                    "er": er,
                    "t_mm": round(t_mm, 4),
                }

        except (ValueError, TypeError):
            error = "Please enter valid numeric values for all fields."

    return render_template("impedance.html", result=result, error=error, form_data=form_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
