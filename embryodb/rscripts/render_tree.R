#!/usr/bin/env Rscript
# LIVEtools lineage-tree renderer.
#
# Driven by `embryodb render-trees-batch`, which resolves every input and
# output path from the DB and hands them over in a manifest. Nothing here
# touches the filesystem to discover work.
#
# Colors reproduce org.rhwlab.tree.HeatMap's gradients step-for-step so trees
# rendered here are comparable to the ones Tree1 has been producing for years:
# the integer truncation is deliberate (black->red tops out at #FE0000, not
# #FF0000), and java.awt.Color.orange is (255,200,0), not R's #FFA500.

suppressPackageStartupMessages({
  library(LIVEtools); library(ggplot2); library(ggtree); library(data.table)
})

java_gradient <- function(one, two, num_steps) {
  c1 <- grDevices::col2rgb(one)[, 1]
  c2 <- grDevices::col2rgb(two)[, 1]
  norm <- (seq_len(num_steps) - 1L) / num_steps
  ch <- vapply(1:3, function(k) as.integer(c1[k] + norm * (c2[k] - c1[k])),
               numeric(num_steps))
  grDevices::rgb(ch[, 1], ch[, 2], ch[, 3], maxColorValue = 255)
}

java_multi_gradient <- function(colors, num_steps) {
  n_sections <- length(colors) - 1L
  per <- num_steps %/% n_sections
  out <- unlist(lapply(seq_len(n_sections),
                       function(s) java_gradient(colors[s], colors[s + 1L], per)))
  if (length(out) < num_steps) {
    out <- c(out, rep(colors[length(colors)], num_steps - length(out)))
  }
  out[seq_len(num_steps)]
}

# Tree1 option mapping: 2 -> black->red on white (its default);
# 1/"rainbow" and 3/"blueyellow" both switch the background to LIGHT_GRAY.
legacy_scheme <- function(name) {
  switch(name,
    blackred = list(colors = java_gradient("black", "red", 500), bg = "#FFFFFF"),
    blueyellow = list(colors = java_gradient("blue", "yellow", 500), bg = "#C0C0C0"),
    rainbow = list(
      colors = java_multi_gradient(
        c("#B520FF", "#0000FF", "#00FF00", "#FFFF00", "#FFC800", "#FF0000"), 500),
      bg = "#C0C0C0"),
    stop("unknown color scheme: ", name))
}

# Tree1 fixes the label font and grows the canvas — one font-size of room per
# terminal branch (`k += kInc` per leaf, then `height = facty * kLast + 100`).
# LIVEtools does the reverse: it shrinks the font to fit a fixed plot_height_in,
# which bottoms out at tip_size_min and turns a 600-cell tree into a grey smear.
# So pin tip_size and derive the tip-axis extent from the tip count instead.
# Because the spacing is pinned to the font, dpi is the only lever left for
# label legibility: 2 mm is ~9 px at 110 dpi (a smear) and ~16 px at 200.
is_tip_layer <- function(layer) {
  d <- layer$data
  inherits(layer$geom, "GeomText") &&
    is.data.frame(d) && all(c("cell", "tree_y") %in% names(d))
}

tip_count <- function(p) {
  for (layer in rev(p$layers)) if (is_tip_layer(layer)) return(nrow(layer$data))
  NA_integer_
}

args <- commandArgs(trailingOnly = TRUE)
opt <- list(manifest = NA, scheme = "rainbow", root = "P0", value_col = "blot",
            linewidth = "3", min_expr = "", max_expr = "", orientation = "vertical",
            width = "16", height = "10", dpi = "200",
            tip_mm = "2", min_tip_axis_in = "8")
for (a in args) {
  kv <- regmatches(a, regexec("^--([^=]+)=(.*)$", a))[[1]]
  if (length(kv) == 3) opt[[gsub("-", "_", kv[2])]] <- kv[3]
}
stopifnot(!is.na(opt$manifest))

sch <- legacy_scheme(opt$scheme)
tip_mm <- as.numeric(opt$tip_mm)
vmin <- if (nzchar(opt$min_expr)) as.numeric(opt$min_expr) else NULL
vmax <- if (nzchar(opt$max_expr)) as.numeric(opt$max_expr) else NULL

jobs <- fread(opt$manifest, sep = "\t", header = TRUE, colClasses = "character")
failed <- 0L

for (i in seq_len(nrow(jobs))) {
  job <- jobs[i]
  cat(sprintf("[%d/%d] %s -> %s\n", i, nrow(jobs), job$series, job$png))
  ok <- tryCatch({
    cd <- as.data.frame(fread(job$csv))
    p <- plot_lineage_tree(
      cd, root = opt$root, value_col = opt$value_col, resolution = "timepoint",
      colors = sch$colors, value_min = vmin, value_max = vmax,
      na_color = sch$bg, branch_width = as.numeric(opt$linewidth),
      end_time = max(cd$time), tip_lab = TRUE, tip_size = tip_mm,
      value_legend = opt$value_col)
    # Tree1 draws time downward with cells across; ggtree defaults to
    # left-to-right, so rotate to match when asked.
    vertical <- opt$orientation == "vertical"
    if (vertical) {
      p <- p + layout_dendrogram()
      # The rotation leaves the tip labels reading across the tips, and its
      # scale_x_reverse discards the headroom plot_lineage_tree had reserved
      # for them, so they clip. Stand them up and hand the room back.
      for (j in seq_along(p$layers)) {
        if (is_tip_layer(p$layers[[j]])) {
          p$layers[[j]]$aes_params$angle <- 90
          p$layers[[j]]$aes_params$hjust <- 1
        }
      }
      p <- p + scale_x_reverse(expand = expansion(mult = c(0.18, 0.02)))
    }
    p <- p + theme(
      plot.background   = element_rect(fill = sch$bg, colour = NA),
      panel.background  = element_rect(fill = sch$bg, colour = NA),
      legend.background = element_rect(fill = sch$bg, colour = NA),
      legend.key        = element_rect(fill = sch$bg, colour = NA))
    n_tips <- tip_count(p)
    tip_axis_in <- if (is.na(n_tips)) NA_real_ else
      max(as.numeric(opt$min_tip_axis_in), n_tips * tip_mm / 25.4)
    # layout_dendrogram() puts the tips along x; otherwise they run down y.
    w <- if (!is.na(tip_axis_in) &&  vertical) tip_axis_in else as.numeric(opt$width)
    h <- if (!is.na(tip_axis_in) && !vertical) tip_axis_in else as.numeric(opt$height)
    dir.create(dirname(job$png), showWarnings = FALSE, recursive = TRUE)
    ggsave(job$png, p, width = w, height = h, dpi = as.numeric(opt$dpi),
           limitsize = FALSE)
    cat(sprintf("      %s tips, %.1f x %.1f in\n", n_tips, w, h))
    TRUE
  }, error = function(e) {
    message(sprintf("  FAILED %s: %s", job$series, conditionMessage(e)))
    FALSE
  })
  if (!ok) failed <- failed + 1L
}

cat(sprintf("rendered %d/%d trees\n", nrow(jobs) - failed, nrow(jobs)))
if (failed > 0L) quit(status = 1L)
