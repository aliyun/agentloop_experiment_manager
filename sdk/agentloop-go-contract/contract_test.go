package agentloopsdkcontract

import (
	"testing"

	agentloop "github.com/alibabacloud-go/agentloop-20260520/v2/client"
)

func TestCreateExperimentRunResponseExposesRecordID(t *testing.T) {
	body := (&agentloop.CreateExperimentRunResponseBody{}).
		SetRecordId("exp-run-contract-test").
		SetStatus("pending").
		SetRequestId("request-contract-test")
	response := (&agentloop.CreateExperimentRunResponse{}).SetBody(body)

	if response.GetBody() == nil || response.GetBody().GetRecordId() == nil {
		t.Fatal("CreateExperimentRunResponseBody must expose recordId")
	}
	if got := *response.GetBody().GetRecordId(); got != "exp-run-contract-test" {
		t.Fatalf("unexpected recordId: %q", got)
	}
}

func TestOfflineCreateRunCanOmitClientToken(t *testing.T) {
	request := (&agentloop.CreateExperimentRunRequest{}).
		SetExperimentPlanId("exp-plan-contract-test").
		SetOfflineExperiments([]*agentloop.OfflineExperimentConfig{
			(&agentloop.OfflineExperimentConfig{}).
				SetName("candidate").
				SetLabel("candidate"),
		})

	if request.GetClientToken() != nil {
		t.Fatal("contract test must not configure clientToken")
	}
	if err := request.Validate(); err != nil {
		t.Fatalf("minimal offline request should validate without clientToken: %v", err)
	}
}
