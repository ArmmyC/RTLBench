module tb;
  reg a;
  reg b;
  wire y;
  integer mismatches;
  TopModule dut(a, b, y);
  initial begin
    mismatches = 0;
    a = 0; b = 0;
    #1;
    if (y !== 0) mismatches = mismatches + 1;
    $display("Mismatches: %0d in 1 samples", mismatches);
    $finish;
  end
endmodule
