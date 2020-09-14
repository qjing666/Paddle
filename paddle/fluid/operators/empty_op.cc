/* Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. */

#include "paddle/fluid/operators/empty_op.h"
#include "paddle/fluid/framework/op_registry.h"

namespace paddle {
namespace operators {

class EmptyOpMaker : public framework::OpProtoAndCheckerMaker {
 public:
  void Make() override {
    AddInput("ShapeTensor",
             "(Tensor<int>), optional). The shape of the output."
             "It has a higher priority than Attr(shape).")
        .AsDispensable();
    AddInput("ShapeTensorList",
             "(vector<Tensor<int>>, optional). The shape of the output. "
             "It has a higher priority than Attr(shape)."
             "The shape of the element in vector must be [1].")
        .AsDuplicable()
        .AsDispensable();
    AddAttr<std::vector<int64_t>>("shape",
                                  "(vector<int64_t>) The shape of the output")
        .SetDefault({});
    AddAttr<int>("dtype", "The data type of output tensor, Default is float")
        .SetDefault(framework::proto::VarType::FP32);
    AddOutput("Out", "(Tensor) The output tensor.");
    AddComment(R"DOC(empty operator
Returns a tensor filled with uninitialized data. The shape of the tensor is
defined by the variable argument shape.


The type of the tensor is specify by `dtype`.
)DOC");
  }
};

class EmptyOp : public framework::OperatorWithKernel {
 public:
  using framework::OperatorWithKernel::OperatorWithKernel;

  void InferShape(framework::InferShapeContext* context) const override {
    OP_INOUT_CHECK(context->HasOutput("Out"), "Output", "Out", "empty");

    if (context->HasInput("ShapeTensor")) {
      auto dims = context->GetInputDim("ShapeTensor");
      int num_ele = 1;
      for (int i = 0; i < dims.size(); ++i) {
        num_ele *= dims[i];
      }

      context->SetOutputDim("Out", framework::make_ddim({num_ele}));
    } else if (context->HasInputs("ShapeTensorList")) {
      std::vector<int> out_dims;
      auto dims_list = context->GetInputsDim("ShapeTensorList");
      for (size_t i = 0; i < dims_list.size(); ++i) {
        auto& dims = dims_list[i];
        PADDLE_ENFORCE_EQ(
            dims, framework::make_ddim({1}),
            "ShapeError: The shape of Tensor in list must be [1]. "
            "But received the shape "
            "is [%s]",
            dims);

        out_dims.push_back(dims[0]);
      }

      context->SetOutputDim("Out", framework::make_ddim(out_dims));
    } else {
      auto& shape = context->Attrs().Get<std::vector<int64_t>>("shape");
      context->SetOutputDim("Out", framework::make_ddim(shape));
    }
  }

 protected:
  framework::OpKernelType GetKernelTypeForVar(
      const std::string& var_name, const framework::Tensor& tensor,
      const framework::OpKernelType& expected_kernel_type) const override {
    if (var_name == "ShapeTensor" || var_name == "ShapeTensorList") {
      return expected_kernel_type;
    } else {
      return framework::OpKernelType(expected_kernel_type.data_type_,
                                     tensor.place(), tensor.layout());
    }
  }

  framework::OpKernelType GetExpectedKernelType(
      const framework::ExecutionContext& context) const override {
    return framework::OpKernelType(
        framework::proto::VarType::Type(context.Attr<int>("dtype")),
        context.GetPlace());
  }
};

class EmptyOpVarTypeInference : public framework::VarTypeInference {
 public:
  void operator()(framework::InferVarTypeContext* context) const override {
    auto data_type = static_cast<framework::proto::VarType::Type>(
        BOOST_GET_CONST(int, context->GetAttr("dtype")));
    context->SetOutputDataType("Out", data_type);
  }
};

}  // namespace operators
}  // namespace paddle

namespace ops = paddle::operators;
namespace plat = paddle::platform;

REGISTER_OPERATOR(
    empty, ops::EmptyOp, ops::EmptyOpMaker, ops::EmptyOpVarTypeInference,
    paddle::framework::EmptyGradOpMaker<paddle::framework::OpDesc>,
    paddle::framework::EmptyGradOpMaker<paddle::imperative::OpBase>);

REGISTER_OP_CPU_KERNEL(empty, ops::EmptyKernel<plat::CPUDeviceContext, bool>,
                       ops::EmptyKernel<plat::CPUDeviceContext, int>,
                       ops::EmptyKernel<plat::CPUDeviceContext, int64_t>,
                       ops::EmptyKernel<plat::CPUDeviceContext, float>,
                       ops::EmptyKernel<plat::CPUDeviceContext, double>,
                       ops::EmptyKernel<plat::CPUDeviceContext, plat::float16>);
