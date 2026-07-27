module TopModule(input wire a, input wire b, output wire y);
  assign y = a & b;
endmodule

module UnrelatedRoot;
  initial $fatal(1, "UNRELATED_ROOT_EXECUTED");
endmodule
