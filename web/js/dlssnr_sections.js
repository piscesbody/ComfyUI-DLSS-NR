import { app } from "../../scripts/app.js";

function makeGroupHeader(name, label) {
    return {
        name,
        type: "DLSSNR_GROUP",
        value: label,
        label: "",
        options: { serialize: false },
        serialize: false,
        _dlssnrHeader: true,
        draw(ctx, node, widget_width, y, H) {
            const r = 4;
            ctx.save();
            // box
            ctx.fillStyle = "#242424";
            ctx.strokeStyle = "#555";
            ctx.lineWidth = 1;
            const bx = 6, bw = widget_width - 12, bh = 22;
            ctx.beginPath();
            ctx.roundRect(bx, y + 3, bw, bh, r);
            ctx.fill();
            ctx.stroke();
            // accent bar
            ctx.fillStyle = "#4fc3f7";
            ctx.fillRect(bx + 1, y + 3 + 4, 3, bh - 8);
            // text
            ctx.fillStyle = "#d8dce8";
            ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
            ctx.textAlign = "left";
            ctx.textBaseline = "middle";
            ctx.fillText(label, bx + 12, y + 3 + bh / 2 + 1);
            ctx.restore();
        },
        computeSize(width) {
            return [width, 28];
        },
    };
}

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

function labels() {
    try {
        return (navigator.language || "en").toLowerCase().startsWith("zh")
            ? LABELS.zh : LABELS.en;
    } catch (e) {
        return LABELS.en;
    }
}

function restyle(node) {
    if (!node?.widgets) return;
    if (node._dlssnrGrouped) return;
    const names = node.widgets.map((w) => w.name);
    if (!names.some((n) => String(n).startsWith("sec_"))) return;
    node._dlssnrGrouped = true;
    const L = labels();
    for (let i = 0; i < node.widgets.length; i++) {
        const w = node.widgets[i];
        if (w.type === "combo" && String(w.name).startsWith("sec_")) {
            const header = makeGroupHeader(w.name, L[w.name] || w.value);
            node.widgets.splice(i, 1, header);  // replace in place
        }
    }
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: "dlssnr.sections",
    nodeCreated(node) {
        if (String(node?.comfyClass || node?.type || "").startsWith("DLSSNR")) {
            restyle(node);
        }
    },
});
