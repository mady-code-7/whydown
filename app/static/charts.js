/* The charts of the site detail page (spec section 8.5).
 *
 * The data comes from the JSON element of the page, the colours come from
 * the CSS variables of the stylesheet, so a chart is readable in the light
 * and in the dark mode.
 *
 * The script is defensive on purpose: when the script of the CDN is
 * blocked, when the data is missing or when the canvas cannot be drawn, a
 * short message is shown instead of a broken chart.
 */
(function () {
  "use strict";

  /* How many of the latest checks the stage chart shows. */
  var STAGE_LIMIT = 30;

  function element(id) {
    return document.getElementById(id);
  }

  function cssColor(name, fallback) {
    try {
      var value = window.getComputedStyle(document.documentElement)
        .getPropertyValue(name);
      value = (value || "").trim();
      return value || fallback;
    } catch (err) {
      return fallback;
    }
  }

  function showMessage(canvasId, noteId, message) {
    var canvas = element(canvasId);
    var note = element(noteId);
    if (canvas) {
      canvas.hidden = true;
    }
    if (note) {
      note.textContent = message;
      note.hidden = false;
    }
  }

  function readData() {
    var node = element("chart-data");
    if (!node) {
      return null;
    }
    try {
      return JSON.parse(node.textContent || "{}");
    } catch (err) {
      return null;
    }
  }

  /* A timing that was never measured stays null: it is a gap in the chart
     and never a zero. */
  function numbers(list) {
    var values = Array.isArray(list) ? list : [];
    return values.map(function (value) {
      return typeof value === "number" && isFinite(value) ? value : null;
    });
  }

  function tooltipDetails(data, index) {
    var checks = Array.isArray(data.checks) ? data.checks : [];
    var check = checks[index];
    if (!check) {
      return "";
    }
    var lines = [];
    if (check.result) {
      lines.push(check.result);
    }
    if (check.error_detail) {
      lines.push(String(check.error_detail));
    }
    return lines.join("\n");
  }

  function axisOptions(stacked) {
    return {
      x: {
        stacked: stacked,
        ticks: {
          autoSkip: true,
          color: cssColor("--muted", "#59636e"),
          maxRotation: 0,
          maxTicksLimit: 8,
        },
        grid: { color: cssColor("--border", "#d0d7de") },
      },
      y: {
        stacked: stacked,
        beginAtZero: true,
        ticks: { color: cssColor("--muted", "#59636e") },
        grid: { color: cssColor("--border", "#d0d7de") },
        title: {
          display: true,
          text: "ms",
          color: cssColor("--muted", "#59636e"),
        },
      },
    };
  }

  function draw(data) {
    var labels = Array.isArray(data.labels) ? data.labels : [];
    var totals = numbers(data.total_ms);
    var stages = data.stages || {};

    new Chart(element("chart-total"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Total",
            data: totals,
            borderColor: cssColor("--chart-total", "#0969da"),
            backgroundColor: cssColor("--chart-total-fill", "rgba(9, 105, 218, 0.12)"),
            borderWidth: 2,
            pointRadius: 0,
            spanGaps: false,
            tension: 0.2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        spanGaps: false,
        scales: axisOptions(false),
        plugins: {
          legend: { labels: { color: cssColor("--text", "#1f2328") } },
          tooltip: {
            callbacks: {
              afterBody: function (items) {
                return tooltipDetails(data, items[0].dataIndex);
              },
            },
          },
        },
      },
    });

    /* The stage chart shows only the latest checks, the line chart shows
       the whole history that was read. */
    var offset = Math.max(labels.length - STAGE_LIMIT, 0);
    var stageLabels = labels.slice(offset);
    var definitions = [
      { key: "dns_ms", label: "DNS", color: "--chart-dns", fallback: "#0969da" },
      { key: "tcp_ms", label: "TCP", color: "--chart-tcp", fallback: "#1a7f37" },
      { key: "tls_ms", label: "TLS", color: "--chart-tls", fallback: "#bc4c00" },
      { key: "ttfb_ms", label: "TTFB", color: "--chart-ttfb", fallback: "#8250df" },
    ];

    new Chart(element("chart-stages"), {
      type: "bar",
      data: {
        labels: stageLabels,
        datasets: definitions.map(function (definition) {
          return {
            label: definition.label,
            data: numbers(stages[definition.key]).slice(offset),
            backgroundColor: cssColor(definition.color, definition.fallback),
            borderWidth: 0,
            spanGaps: false,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: axisOptions(true),
        plugins: {
          legend: { labels: { color: cssColor("--text", "#1f2328") } },
          tooltip: {
            callbacks: {
              afterBody: function (items) {
                return tooltipDetails(data, items[0].dataIndex + offset);
              },
            },
          },
        },
      },
    });
  }

  function main() {
    var data = readData();
    if (!data) {
      showMessage(
        "chart-total",
        "chart-total-note",
        "The charts could not be loaded."
      );
      showMessage(
        "chart-stages",
        "chart-stages-note",
        "The charts could not be loaded."
      );
      return;
    }

    var labels = Array.isArray(data.labels) ? data.labels : [];
    if (!labels.length) {
      showMessage("chart-total", "chart-total-note", "There is no data to chart yet.");
      showMessage("chart-stages", "chart-stages-note", "There is no data to chart yet.");
      return;
    }

    if (typeof Chart === "undefined") {
      showMessage(
        "chart-total",
        "chart-total-note",
        "The charts could not be loaded: the chart library is not available."
      );
      showMessage(
        "chart-stages",
        "chart-stages-note",
        "The charts could not be loaded: the chart library is not available."
      );
      return;
    }

    try {
      draw(data);
    } catch (err) {
      showMessage("chart-total", "chart-total-note", "The chart could not be drawn.");
      showMessage("chart-stages", "chart-stages-note", "The chart could not be drawn.");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
