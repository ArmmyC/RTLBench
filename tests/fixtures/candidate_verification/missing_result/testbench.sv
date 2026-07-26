module tb;
  wire y;
  TopModule dut(y);
  initial begin
    #1;
    $display("simulation completed without a result report");
    $finish;
  end
endmodule
