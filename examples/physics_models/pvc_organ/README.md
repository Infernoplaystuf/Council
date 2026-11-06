# PVC pipe organ

Cut lengths for every note of a scale, for open or stopped organ pipes or for struck chimes.

* **Design sheet:** the cut list (note, target frequency, length in mm and in feet and inches, overtones), the total pipe and how many 10 ft sticks.
* **Physics:** `physics_kit.acoustics`, which covers pipe resonance with end correction (Levine & Schwinger) and free-free tube bending modes for chimes.
* **Calibrate:** cut one or two pipes, measure them with a tuner, and put length and frequency in a table (`sample_data/measured_pipes.csv` shows the format). Calibrate fits the end correction, or the tube stiffness for chimes.

The sample data is made up to show the format. Replace the link with your own folder, a Ledger or a Connections graph in the Model view.

Run the tests with **Test** in the Build tab. Open the **Model** view to set the inputs and see the design sheet.
