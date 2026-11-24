# Battery cycle segmentation contract

Cell-test data engineers provide 2..100000 samples with globally unique ID, strictly increasing nonnegative time in seconds, positive voltage and signed current. Numeric fields are decimal strings represented as exact rationals. Positive current at or above active_current_a is charge, negative current at or below its negative is discharge, and the dead band is rest.

Each interval inherits the state of its left sample. Adjacent intervals with the same state form one phase. Amp-hours and watt-hours use trapezoidal current and voltage averages over the interval. Active phases shorter than min_phase_seconds remain in output with too_short status. Total discharge energy is reported as positive magnitude; round-trip efficiency is null when there is no charge energy.

This is a structured laboratory data product, not battery control, health advice, a fitness tracker or a generic time-series dashboard. The baseline does not estimate capacity fade, temperature effects, state of charge or electrochemical safety. Future cycle pairing can build on stable phase identities and energy evidence.

