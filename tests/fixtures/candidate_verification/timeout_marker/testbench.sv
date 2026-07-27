module tb;
  wire y;
  TopModule dut(y);
  initial begin
    #1;
    $display("TIMEOUT");
    $finish;
  end
endmodule
