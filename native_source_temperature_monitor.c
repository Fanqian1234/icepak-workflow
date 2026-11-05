#include "udf.h"
#include <math.h>
#include "nci_source_temperature_ids.h"

/* Read-only C_T / C_VOLUME reductions. No solver field is written. */
static int monitor_layer(int id)
{
    int i;
    for (i = 0; i < NCI_BOTTOM_COUNT; ++i)
        if (id == nci_bottom_ids[i]) return 0;
    for (i = 0; i < NCI_TOP_COUNT; ++i)
        if (id == nci_top_ids[i]) return 1;
    return -1;
}

DEFINE_ON_DEMAND(nci_source_temperatures)
{
#if !RP_HOST
    Domain *domain = Get_Domain(1);
    Thread *thread;
    cell_t cell;
    int layer, i, invalid = 0;
    int count[2] = {0, 0};
    real weighted[2] = {0.0, 0.0};
    real volume[2] = {0.0, 0.0};
    real maximum[2] = {-1.0e30, -1.0e30};
    thread_loop_c(thread, domain)
    {
        layer = monitor_layer(THREAD_ID(thread));
        if (layer < 0) continue;
        begin_c_loop_int(cell, thread)
        {
            real v = C_VOLUME(cell, thread);
            real value = C_T(cell, thread);
            if (!(v > 0.0) || !isfinite(v) || !isfinite(value))
                ++invalid;
            else
            {
                weighted[layer] += value * v;
                volume[layer] += v;
                if (value > maximum[layer]) maximum[layer] = value;
                ++count[layer];
            }
        }
        end_c_loop_int(cell, thread)
    }
#if RP_NODE
    invalid = PRF_GISUM1(invalid);
    for (i = 0; i < 2; ++i)
    {
        weighted[i] = PRF_GRSUM1(weighted[i]);
        volume[i] = PRF_GRSUM1(volume[i]);
        maximum[i] = PRF_GRHIGH1(maximum[i]);
        count[i] = PRF_GISUM1(count[i]);
    }
#endif
    if (invalid ||
        (NCI_BOTTOM_COUNT && (!count[0] || !(volume[0] > 0.0))) ||
        (NCI_TOP_COUNT && (!count[1] || !(volume[1] > 0.0))))
        Error("NCI_SOURCE_TEMPERATURE_INVALID_NATIVE_CELLS\n");
    Message0("\nNCI_SOURCE_MONITOR_BINDING=%s\n", NCI_SOURCE_BINDING);
    Message0("NCI_SOURCE_MONITOR_CELLS bottom=%d top=%d volume_bottom=%.17g volume_top=%.17g\n",
             count[0], count[1], volume[0], volume[1]);
    Message0("THERMAL_MONITOR_BEGIN\n");
    if (NCI_BOTTOM_COUNT) {
        Message0("THERMAL_MONITOR_bottom_volume-avg\nNet %.17g\n", weighted[0] / volume[0]);
        Message0("THERMAL_MONITOR_bottom_maximum\nNet %.17g\n", maximum[0]);
    }
    if (NCI_TOP_COUNT) {
        Message0("THERMAL_MONITOR_top_volume-avg\nNet %.17g\n", weighted[1] / volume[1]);
        Message0("THERMAL_MONITOR_top_maximum\nNet %.17g\n", maximum[1]);
    }
    Message0("THERMAL_MONITOR_END\n");
#endif
}
