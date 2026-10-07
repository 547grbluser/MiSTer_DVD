# clock_check.tcl — worst INTRA-domain slack for every clock, at every corner.
#
# Run via the wrapper (handles Docker and the policy pass):  tools/clock_check.sh
# or directly:                                             quartus_sta -t tools/clock_check.tcl
#
# Why this exists (docs/timing.md): the Fmax/Setup/Hold Summary panels in DVD.sta.rpt mix
# a clock's own paths with every crossing INTO it. The crossings between sys_pll outputs are
# timed on purpose (sys_top.sdc, docs/history.md §10) and their negative slack is expected,
# so the summaries cannot say whether a domain's own logic closes. That blind spot hid the
# clk_mem miss until PR #157. Here every query is -from_clock X -to_clock X.
#
# Output: output_files/clock_check.tsv, one row per (corner, clock, analysis):
#   corner  clock  period_ns  analysis  npaths  slack_ns  from_node  to_node
# npaths is 0 (and slack/nodes are "-") when the domain has no paths of that kind.
# Needs a completed fit on disk (db/ + output_files/); it does not refit.

set rev DVD
set out output_files/clock_check.tsv

project_open $rev -revision $rev
create_timing_netlist
read_sdc
update_timing_netlist

set fh [open $out w]
puts $fh "corner\tclock\tperiod_ns\tanalysis\tnpaths\tslack_ns\tfrom_node\tto_node"

foreach_in_collection op [get_available_operating_conditions] {
    set_operating_conditions $op
    update_timing_netlist
    set corner [get_operating_conditions_info $op -display_name]

    foreach_in_collection clk [get_clocks] {
        set name   [get_clock_info $clk -name]
        set period [get_clock_info $clk -period]
        foreach kind {setup hold recovery removal} {
            set paths [get_timing_paths -$kind -from_clock $clk -to_clock $clk -npaths 1]
            set n [get_collection_size $paths]
            if { $n == 0 } {
                puts $fh "$corner\t$name\t$period\t$kind\t0\t-\t-\t-"
                continue
            }
            foreach_in_collection p $paths {
                set slack [get_path_info $p -slack]
                set from  [get_node_info -name [get_path_info $p -from]]
                set to    [get_node_info -name [get_path_info $p -to]]
                puts $fh "$corner\t$name\t$period\t$kind\t$n\t$slack\t$from\t$to"
            }
        }
    }
}

close $fh
post_message -type info "clock_check: wrote $out"
project_close
