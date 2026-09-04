import { app } from "../../scripts/app.js";

// Section bars are decorative. They must NOT occupy serialized value slots,
// otherwise legacy (pre-grouping) workflows misalign. Strategy:
//   - keep sec_* combos in node.widgets so the schema stays stable
//   - mark them non-serializing and draw them as styled bars
//   - legacy workflows are detected by value count and migrated by name

const LABELS = {
    zh: {
        sec_preset: "画质预设",
        sec_size: "输出尺寸",
        sec_nr: "NR 增强 — 细节 / 色调 / 皮肤",
        sec_enc: "运动与编码",
    },
    en: {
        sec_preset: "PRESET",
        sec_size: "OUTPUT SIZE",
        sec_nr: "NR ENHANCE — detail / tone / skin",
        sec_enc: "MOTION & ENCODING",
    },
};

function locale() {
    try {
        return (navigator.language || "en").toLowerCase().startsWith("zh")
            ? LABELS.zh : LABELS.en;
    } catch (e) {
        return LABELS.en;
    }
}

// Legacy layouts: saved-value count -> ordered widget names those values belong to.
// v0.1 video node had 14 widgets (runtime / nr_preset / preserve_audio / prefix era);
// v0.2 video node had 17 (identical to the current first 17). The IMAGE node
// saves no value for its images input, hence the offset entry below.
const LEGACY_NAMES = {
    DLSSNRVideoUpscale: {
        14: ["video_path", "runtime", "upscale_factor", "output_width",
             "nr_style", null, "nr_intensity", "nr_detail", "nr_color",
             "motion_engine", "codec", "cq", null, null],
        17: ["video_path", "quality_preset", "upscale_factor", "output_width",
             "nr_style", "nr_intensity", "nr_detail", "nr_color", "nr_skin",
             "nr_structure", "nr_tone", "nr_global_tone", "auto_mask",
             "motion_engine", "gpu_adapter", "codec", "cq"],
        // v0.4 / v0.4.2: 21 values = 17 params + 4 sec_* nulls, saved in
        // restyled (schema) order; output_width sits between factor and sec_nr
        21: ["video_path", null, "quality_preset", null, "upscale_factor",
             "output_width", null, "nr_style", "nr_intensity", "nr_detail",
             "nr_color", "nr_skin", "nr_structure", "nr_tone",
             "nr_global_tone", "auto_mask", null, "motion_engine",
             "gpu_adapter", "codec", "cq"],
    },
    DLSSNRImageUpscale: {
        9: [null, "runtime", "upscale_factor", "output_width", "nr_style",
            "nr_preset", "nr_intensity", "nr_detail", "nr_color"],
        15: [null, "quality_preset", "upscale_factor", "output_width",
             "batch_mode", "self_check", "nr_style", "nr_intensity",
             "nr_detail", "nr_color", "nr_skin", "nr_structure", "nr_tone",
             "nr_global_tone", "auto_mask"],
        // v0.4 / v0.4.2: section bars serialized as null in schema order
        // (17 = 14 params + sec_preset/sec_size/sec_nr nulls)
        17: [null, "quality_preset", null, "upscale_factor", "output_width",
             null, "batch_mode", "self_check", "nr_style", "nr_intensity",
             "nr_detail", "nr_color", "nr_skin", "nr_structure", "nr_tone",
             "nr_global_tone", "auto_mask"],
    },
};

function repairLegacyValues(node) {
    const cls = String(node.comfyClass || node.type || "");
    const table = LEGACY_NAMES[cls];
    if (!table) return false;
    const vals = node.widgets_values;
    if (!Array.isArray(vals)) return false;
    const names = table[vals.length];
    if (!names) return false; // current format or unknown - leave untouched

    const byName = {};
    names.forEach((name, i) => {
        if (name) byName[name] = vals[i];
    });

    node.widgets?.forEach((w) => {
        if (String(w.name).startsWith("sec_")) return;
        if (!(w.name in byName)) {
            // widget did not exist in the legacy layout (e.g. sr_preset in a
            // v0.4 file): litegraph position-assigned a stray value. Reset to
            // the Python-side default captured on a fresh node build.
            const fresh = DEFAULTS[cls]?.[w.name];
            if (fresh !== undefined) w.value = fresh;
            return;
        }
        let v = byName[w.name];
        if (w.name === "motion_engine" && typeof v === "number") v = "auto"; // 0/1 -> auto/nvof/lk
        if (w.name === "nr_style" && !String(v).includes(" ")) {
            const n = parseInt(v);
            if (!isNaN(n)) v = n + " Default";
        }
        if (w.value !== v) { w.value = v; }
    });

    // rewrite serialized values in current layout order (sec_* excluded)
    node.widgets_values = node.widgets
        .filter((w) => !String(w.name).startsWith("sec_"))
        .map((w) => w.value);
    node.setDirtyCanvas?.(true, true);
    console.log("[DLSS-NR] migrated legacy workflow values:", cls, vals.length);
    return true;
}

// Widgets are saved in the restyled (schema) order with null at each sec_*
// slot, but litegraph assigns widgets_values in its own build order, where
// the frontend pushes sec_* widgets to the END of node.widgets. Positions
// therefore disagree and every load scrambles the parameters. Detect the
// current format by that null pattern and restore strictly by NAME.
const SCHEMA_ORDER = ["video", "video_path", "sec_preset", "quality_preset",
    "sec_size", "upscale_factor", "output_width", "sec_nr", "nr_style",
    "sr_preset", "nr_intensity", "nr_detail", "nr_color", "nr_skin",
    "nr_structure", "nr_tone", "nr_global_tone", "auto_mask", "batch_mode",
    "self_check", "sec_enc", "motion_engine", "gpu_adapter", "codec",
    "bit_depth", "cq", "bitrate", "enc_preset", "audio_mode", "audio_bitrate"];

const rankOf = (w) => {
    const i = SCHEMA_ORDER.indexOf(String(w.name));
    return i === -1 ? 999 : i;
};

// Python-side defaults for widgets added after v0.4.2, used when migrating
// legacy workflows whose value arrays never contained them.
const DEFAULTS = {
    DLSSNRVideoUpscale: { sr_preset: "default", bit_depth: "10", cq: 19,
                          bitrate: 0, enc_preset: "p5", audio_bitrate: 192 },
    DLSSNRImageUpscale: { sr_preset: "default" },
};

function restoreCurrentFormat(node) {
    const vals = node.widgets_values;
    if (!Array.isArray(vals) || !Array.isArray(node.widgets)) return false;
    if (!node.widgets.some((w) => String(w.name).startsWith("sec_"))) return false;

    const namesR = [...node.widgets]
        .sort((a, b) => rankOf(a) - rankOf(b))
        .map((w) => String(w.name));
    if (vals.length !== namesR.length) return false;
    const patternOk = namesR.every((n, i) => {
        const v = vals[i];
        if (n.startsWith("sec_")) {
            return v === null || v === undefined ||
                (typeof v === "string" && v.startsWith("━━"));
        }
        return v !== null && v !== undefined;
    });
    if (!patternOk) return false;

    const byName = {};
    namesR.forEach((n, i) => {
        if (!n.startsWith("sec_")) byName[n] = vals[i];
    });
    node.widgets.forEach((w) => {
        if (String(w.name).startsWith("sec_")) return;
        if (w.name in byName && w.value !== byName[w.name]) w.value = byName[w.name];
    });
    node.widgets_values = node.widgets
        .filter((w) => !String(w.name).startsWith("sec_"))
        .map((w) => w.value);
    node.setDirtyCanvas?.(true, true);
    console.log("[DLSS-NR] restored parameters by name (", namesR.length, "values)");
    return true;
}

function styleSectionWidget(w) {
    if (w._dlssnrBar) return;
    w._dlssnrBar = true;
    try { w.serialize = false; } catch (e) {}
    if (w.options && typeof w.options === "object") w.options.serialize = false;
    w.computeSize = (width) => [width, 28];
    w.draw = function (ctx, node, widget_width, y, H) {
        const label = (locale())[this.name] || this.value || this.name;
        ctx.save();
        ctx.fillStyle = "#242424";
        ctx.strokeStyle = "#555";
        ctx.lineWidth = 1;
        const bx = 6, bw = widget_width - 12, bh = 22;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx, y + 3, bw, bh, 4);
        else ctx.rect(bx, y + 3, bw, bh);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#4fc3f7";
        ctx.fillRect(bx + 1, y + 7, 3, bh - 8);
        ctx.fillStyle = "#d8dce8";
        ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(label, bx + 12, y + 3 + bh / 2 + 1);
        ctx.restore();
    };
}

function restyle(node) {
    if (!node?.widgets || node._dlssnrBarDone) return;
    const bars = {};
    const rest = [];
    for (const w of node.widgets) {
        if (String(w.name).startsWith("sec_")) {
            styleSectionWidget(w);
            bars[w.name] = w;
        } else {
            rest.push(w);
        }
    }
    if (!Object.keys(bars).length) return;
    // reassemble in schema order
    const merged = [...rest, ...Object.values(bars)]
        .sort((a, b) => rankOf(a) - rankOf(b));
    node.widgets.length = 0;
    node.widgets.push(...merged);
    node._dlssnrBarDone = true;
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: "dlssnr.sections",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (!String(nodeData?.name || "").startsWith("DLSSNR")) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            restyle(this);
            return r;
        };
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            if (!repairLegacyValues(this)) restoreCurrentFormat(this);
            restyle(this);
            return r;
        };
    },
});
