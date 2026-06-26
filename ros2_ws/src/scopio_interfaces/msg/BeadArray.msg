# All beads detected in one frame (the microscope's primary "sensor" output).
std_msgs/Header header
int32 count                    # number of beads in `beads`
int32 clump_count              # features flagged as clumps (overlapping beads)
scopio_interfaces/Bead[] beads
