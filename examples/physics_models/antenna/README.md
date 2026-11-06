# Radio antenna

A dipole, quarter-wave ground plane or Yagi for a frequency.

* **Design sheet:** element lengths (mm and ft-in), boom positions, impedance, SWR on your line, gain, front-to-back, the 2:1 bandwidth and the pattern.
* **Physics:** `physics_kit.antennas`, a thin-wire method-of-moments solver. Its half-wave dipole checks out at 72 Ω and 2.14 dBi.
* **Data:** an antenna analyzer sweep with frequency and SWR columns (`sample_data/analyzer_sweep.csv`). The model says where the antenna really resonates and how much to trim.

The sample data is made up to show the format. Replace the link with your own folder, a Ledger or a Connections graph in the Model view.

Run the tests with **Test** in the Build tab. Open the **Model** view to set the inputs and see the design sheet.
