/* Copyright 2022 The TensorFlow Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#ifndef FUSION_INTERFACE
#define FUSION_INTERFACE

include "mlir/IR/OpBase.td"

def FusionIterface : OpInterface<"FusionIterface"> {
  let description = [{
    TBD
  }];
  let cppNamespace = "::mlir::gml_st";

  let methods = [
    InterfaceMethod<
      /*desc=*/"TBD",
      /*retTy=*/"::mlir::Value",
      /*methodName=*/"fuse",
      /*args=*/(ins
          "::mlir::Location":$loc,
          "::mlir::Value":$subset,
          "OpBuilder&":$builder),
      /*methodBody=*/"",
      /*defaultImplementation=*/[{
        return {};
      }]>,
  ];
}

#endif // FUSION_INTERFACE
