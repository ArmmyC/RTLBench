module tb;
  reg a;
  reg b;
  wire y;
  integer mismatches;
  top dut(a, b, y);
  task check;
    input expected;
    begin
      #1;
      if (y !== expected) mismatches = mismatches + 1;
    end
  endtask
  initial begin
    mismatches = 0;
    a = 0; b = 0; check(0);
    a = 0; b = 1; check(0);
    a = 1; b = 0; check(0);
    a = 1; b = 1; check(1);
    $display("Mismatches: %0d", mismatches);
    if (mismatches != 0) $fatal(1, "functional failure");
    $finish;
  end
endmodule
